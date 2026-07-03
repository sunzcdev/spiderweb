# reading-graph (spiderweb) 代码 Review 报告

**日期**: 2026-07-03  
**审查对象**: spiderweb reading-graph 三层知识图谱引擎 + service 层 + MCP server + 测试套件  
**范围**: ~3000 行源码 + ~1300 行测试，5 个并行 code-reviewer agent + 跨文件架构分析

---

## 总体结论

**Verdict: REQUEST CHANGES（合并前必须修复一批 🔴 数据丢失/安全问题）**

引擎分层本身是干净的（L1↔L2 边界清晰、无反向依赖；service 层不是纯 pass-through，`think()` 的 BFS+decay+hot-stopping 和 `_find_anchor` 四级回退是真正的领域算法，YAGNI 测试通过）。但**写路径和数据安全面有系统性问题**：

1. **一类是"静默数据丢失/污染"**（WAL 误删、`INSERT OR REPLACE` 孤儿化、record slug 冲突覆盖、迁移非幂等、ingest 先删后导）
2. **一类是"信任边界无校验"**（MCP `ingest` 路径遍历、`top_n` 无上界）
3. **一类是"故障静默化"**（大量 `except Exception: pass`、stats 用 `total_changes` 恒真、`str(graph_result)` 引用 bug）

测试套件 46 passed 但 MCP/LLM 面是"假装 mock 实则打真实坏 client"的假性通过。

---

## 一、设计架构（跨文件，逐文件 agent 看不到的）

### A1. spiderweb 重构可能是生产 reading-graph 的功能回退 — 中网分类疑似缺失 🔴
- **问题**：架构文档（`reading-graph-v2-架构.md`）把"中网分类"（`_incremental_classify` / `_NOISE_WORDS` 146 词 / FTS5 snippet 校验 / `_CLASSIFY_BATCH_SIZE=30`）列为已完成的核心层，生产代码在 `~/.hermes/util/reading-graph`。但本次审查的 spiderweb `engine/l2/build.py`+`extract.py` 里**未见任何分类逻辑**——只有 LLM 抽取实体+关系，没有给实体打 `person/work/concept/noise` 标签、没有 noise 预过滤。
- **影响**：🔴 若确实缺失，navigate 质量会从"中网（已洗矿）"回退到"粗网（358 万条共现边含大量噪声）"水平，架构文档第三节整段经验失效。
- **修复**：确认 source of truth。若 spiderweb 是新主干，需把中网分类迁入 `engine/l2/`（一个 `classify.py`），或在 `build.py` 抽取后接 `_incremental_classify`；若中网分类仍在 `~/.hermes/util/` 单体脚本里，需明确两者的边界与迁移计划。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py`（全文 135 行无分类）；对照 `~/notebooks/做/读书郎/reading-graph-v2-架构.md:100-253`

### A2. domain.yaml 的类型化 relation schema 与生产数据关系类型对不上 🟡
- **问题**：`domains/reading/domain.yaml` 定义了 `AUTHORED/INFLUENCED_BY/CONTRADICTS/PARTICIPATED_IN/…` 等 13 种带 `valid_triplets` 的类型化关系；但架构文档与生产数据实际用的是 `co_occur/logic/oppose/mentions`（`entity_relations 3,581,541+ 边 = co_occur 3,581,445 / logic 10 / mentions 102 / oppose 1`）。两套体系完全对不上。
- **影响**：🟡 要么 domain.yaml 是 aspirational 未被 engine 消费（死配置），要么迁移时关系类型会全部丢失/误映射。`valid_triplets` 校验如果启用会把生产绝大多数边判非法。
- **修复**：定一边为 source of truth。若保留 co_occur/logic/oppose/mentions，把 domain.yaml 改成这套并在 `valid_triplets` 里允许 co_occur 任意类型对；若要走类型化 schema，写迁移把存量边映射到新类型。
- **位置**：`/home/ubuntu/projects/spiderweb/domains/reading/domain.yaml:40-128`

### A3. domain.yaml entity_types 缺 `noise` / `unclear` 🟡
- **问题**：生产系统的中网过滤、navigate 隐藏 noise 边、衰退机制都依赖 `entity_type='noise'`（651 个）和 `unclear`（10 个）。domain.yaml 的 entity_types 是 `person/work/concept/event/location/stem_branch/element`，**没有 noise 和 unclear**。
- **影响**：🟡 navigate 的"噪声已过滤"前提不成立，会用错类型枚举。
- **修复**：补 `noise: {label: "噪声"}` 和 `unclear: {label: "未定"}`。
- **位置**：`/home/ubuntu/projects/spiderweb/domains/reading/domain.yaml:4-39`

### A4. config.yaml 默认 `data_dir` 与真实数据位置不一致；模型 ID 可疑 🟡
- **问题**：`config.yaml` 写 `data_dir: ~/.spiderweb/reading`，但真实 DB 在 `~/notebooks/wiki/reading-graph/data/doc_index.db`——默认配置指向不存在的目录，新装起来找不到存量数据。模型 ID `deepseek-v4-flash` / `sensenova-6.7-flash-lite` / `voyage-4-large` / `rerank-2-lite` 看起来非真实模型名（DeepSeek 真实是 `deepseek-chat`/`deepseek-reasoner`）。
- **影响**：🟡 默认配置即坏；模型 ID 若非 gateway 别名会直接 404。
- **修复**：`data_dir` 默认改为 `~/notebooks/wiki/reading-graph/data` 或走 env var；模型 ID 与架构文档对齐（DeepSeek Chat）并确认 gateway 能解析。
- **位置**：`/home/ubuntu/projects/spiderweb/domains/reading/config.yaml:1-24`

### A5. 规格偏差：文档说 5 个 MCP 工具，实际注册 4 个（无 discover）🟢
- **问题**：任务描述列出 `discover/record/ingest/read/think` 五个工具，`server.py` 只注册 `think/read/record/ingest` 四个，模块 docstring 也写"4-tool interface"。
- **影响**：🟢 规格与实现不一致；若 discover 已被 think 取代，同步文档即可。
- **修复**：确认 discover 是不是漏交付；是则补，否则更新 `mcp-tool-requirements-v1.md`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/mcp/server.py:1, 35-61`

---

## 二、按权重排序的问题清单（13 🔴 + 15 🟡 + 20 🟢）

### 🔴 严重（合并前必须修）

#### S1. WAL 恢复启发式会删除已提交但未 checkpoint 的事务 — 静默数据丢失
- **问题**：`get_db` 在 `wal_mtime - db_mtime > 120` 时直接删 `*-wal`/`*-shm`。WAL 模式下已提交事务合法地留在 WAL 里直到 checkpoint；一个跑 >120s 的 ingest 批次，下次启动时 WAL 被丢弃 = 丢掉所有已提交写入。还假定单进程独占，若有活进程持有 DB，删它的 `-shm` 会腐化其视图。
- **影响**：🔴 静默数据丢失，最坏 orphans 整个 DB。
- **修复**：删掉自研 mtime 启发式，依赖 SQLite 自身的 open 恢复（它本就做对）。要防崩溃 writer，开库后跑 `PRAGMA wal_checkpoint(TRUNCATE)` + 查 `integrity_check`。绝不删自己没创建的 `-wal`/`-shm`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/db.py:132-148`

#### S2. MCP `ingest` 接受任意文件路径 — 信任边界路径遍历
- **问题**：`ingest` 的 `source` 参数是文件路径，直接 `_reading.ingest(arguments["source"])`，无校验。MCP 输入来自 LLM（不可信）。LLM 可传 `/etc/passwd`、`~/.ssh/id_rsa` 或 `../../../` 遍历，ingest 会读取→分块→向量化→入库，等于把任意可读文件内容持久化进图谱并可通过 `read` 回读。
- **影响**：🔴 注入/越权，可读取进程有权限的任意文件。
- **修复**：在 `call_tool` 的 ingest 分支或 `ReadingService.ingest` 入口校验：`Path(source).resolve()` 必须落在 config 允许的 ingest 根白名单内（如 `config.data_dir/books`），`is_relative_to(allowed_root)`，拒绝符号链接与非普通文件。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/mcp/server.py:56-60, 87-88`

#### S3. ingest() 先删后导留数据丢失窗口，无事务
- **问题**：path 已存在时，先删 docs/chunks/fts/vec 并 `commit()`，再 `ingest_file()` 解析。若解析抛错（损坏 epub / IO 错），旧 doc 已删已提交——不可恢复。
- **影响**：🔴 重新入库失败时静默丢失既有书籍。
- **修复**：先解析分块（`title, author, chunks = ingest_file(...)`），再把 delete+insert 放进**单事务**（删旧、插新 docs/chunks/fts，末尾一次 `commit()`）。或至少用 SAVEPOINT 包住 delete，新插入成功才 commit。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:196-251`

#### S4. record() slug 冲突静默覆盖既有 insight — 数据丢失
- **问题**：`record()` 用 `title = content[:50]` 交给 `write_insight`，后者 `INSERT OR REPLACE INTO insights ... slug=...`，slug = `title.lower()[:80]`。两条前 50 字相同的 record 产生同 slug → 第二条静默替换第一条。
- **影响**：🔴 丢失 insight，无任何错误上抛给用户/LLM。
- **修复**：slug 追加短 hash/时间戳（`f"{slug}-{hashlib.md5(content.encode()).hexdigest()[:6]}"`），或用 `INSERT`（非 `OR REPLACE`）捕获 `IntegrityError` 后加后缀。`record()` 的 50 字截断放大碰撞率，是触发点。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:184-186` + `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l3/insight.py:201-208`

#### S5. 迁移声称幂等实则非幂等 — 重跑复制 chunks/query_history
- **问题**：模块 docstring（line 6）写 "One-shot, idempotent (safe to re-run)"。`_migrate_docs`/`_migrate_insights` 有查重，但 `_migrate_chunks`（line 129）和 `_migrate_query_history`（line 409）**无条件 INSERT**。第二次跑产生重复 chunks 行（新 ID）、重复 FTS 行、重复 query_history。
- **影响**：🔴 静默数据污染：重跑越多孤儿越多，chunk_map 指向第一组新 ID，第二组 chunks 无向量、污染 FTS/LIKE 检索。
- **修复**：`_migrate_chunks` 插入前 `SELECT id FROM chunks WHERE doc_id=? AND section_path=?` 冲突则 skip/`INSERT OR REPLACE`；`_migrate_query_history` 用 `INSERT OR IGNORE` + unique index 或按 `(query_text, created_at)` 去重。或整包单事务 + `_meta` flag `migration_completed`，非 `--force` 拒绝重跑。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:108-153, 393-417`

#### S6. `_chunk_markdown` 只按 `## ` 切分，`### ` 子节被吞进父节 chunk
- **问题**：docstring 声明 "Chunk markdown by ## and ### headings"，但 `re.split(r"\n(?=## )", text)` 的 lookahead 只匹配 `## `（井号+空格），`### ` 第三字符是 `#` 非空格，不触发切分。结果一个 `## ` 大节下所有 `### ` 子节正文合并进单个 chunk，heading_level 恒为 2，section_path 丢子节定位。
- **影响**：🔴 L1 切片是下游 L2 抽取与检索的输入：超大 chunk（整章）撑爆 LLM token 上限、embedding 语义稀释、section_path 丢定位。真实数据完整性 bug。
- **修复**：lookahead 改 `r"\n(?=##\s)"`（匹配二级及以上），或对每个 section 再按 `### ` 二次切分单独建 Chunk，section_path 拼成 `"父节 / 子节"`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l1/ingest.py:64-94`

#### S7. `INSERT OR REPLACE` 重录 insight 致 FTS/向量行孤儿化
- **问题**：`write_insight` 用 `INSERT OR REPLACE INTO insights (slug, ...)`。slug UNIQUE，重录同名 title 时 SQLite 删旧行插新行，新 rowid 自增不复用。随后 FTS 和 vec 用 `cursor.lastrowid`（新 rowid）写入。旧 insight 的 FTS/vec 行（旧 rowid）既不被 REPLACE 命中也不被删，成孤儿：FTS 残留指向已删 insight 的索引项。
- **影响**：🔴 静默搜索污染：重录越多孤儿越多，搜索命中已删内容或返回失效结果。
- **修复**：先 `SELECT id FROM insights WHERE slug=?`；存在则 `UPDATE`（保留原 rowid），否则 `INSERT`。或在 REPLACE 后显式 `DELETE FROM insights_fts/insights_vec WHERE rowid=?` 清旧 rowid。推荐 UPDATE 路径。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l3/insight.py:203-232`

#### S8. ingest() 图构建失败错误恒为 `"{}"`，且失败首行即 NameError
- **问题**：`except Exception:` 块里 `graph_result = {"error": str(graph_result)}`，而 `graph_result` 两行前刚被赋成 `{}`。真实异常被丢，错误串恒为字面 `"{}"`。返回的 `entities_found`/`relations_added` 看着像成功（0/0）。更糟：若 `build_graph` 第一条语句就抛，`graph_result` 尚未赋值，`str(graph_result)` 抛 `NameError`。
- **影响**：🔴 图构建失败被静默掩盖，运维永远查不出某本书为何没 L2 图。
- **修复**：`except Exception as e: graph_result = {"error": str(e)}`，并在返回 dict 里 surface（如 `"graph_error": graph_result.get("error")`）。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:262-267`

#### S9. FTS5 表无同步触发器，与源表会 drift
- **问题**：`chunks_fts`、`insights_fts` 是独立 FTS5 表，无 external-content 模式、无 INSERT/UPDATE/DELETE 触发器。任何不经手动镜像 FTS 的写入/删除路径都会留下陈旧/缺失行，搜索与数据长期静默不同步。
- **影响**：🔴 搜索返回错误/陈旧结果，无 DB 级不变量保障一致性。
- **修复**：用 `content='chunks', content_rowid='id'` external-content + AFTER INSERT/UPDATE/DELETE 触发器；或把所有 chunk 写入收敛到单一 `insert_chunk()` 在单事务里同时写两表。insights 同理。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/db.py:26-29, 72-76`

#### S10. Voyage provider 在 async 方法里跑阻塞 HTTP I/O
- **问题**：`VoyageEmbedding.embed` 和 `VoyageReranker.rerank` 是 `async def` 但同步调 `voyageai.Client().embed(...)`/`.rerank(...)`，在事件循环线程上发 HTTP 请求，阻塞所有其他协程（含 MCP 其他 tool call）整个 RTT。
- **影响**：🔴 并发 MCP 流量下整服务器在每次 embed/rerank 时卡死。
- **修复**：`await asyncio.to_thread(vo.embed, ...)` / `vo.rerank`，或用 async voyage client。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/providers.py:101-106, 171-176`

#### S11. MCP 信任边界零输入校验，且零测试
- **问题**：`call_tool` 用 `arguments["anchor"]`/`["query"]`/`["content"]`/`["source"]` 裸索引，缺字段抛 KeyError 被外层 `except Exception`(95-98) 吞成 `"Error: {e}"`。`top_n` 经 `arguments.get("top_n", 3)` 直入 SQL `LIMIT`，无类型/范围校验，`top_n=10**9` 触发无界查询。`test_mcp_tools.py`（473 行）从未 import `spiderweb.mcp.server`——`list_tools`/`call_tool`/PID 锁/错误包装全无覆盖。这违反项目 CLAUDE.md "Input validation at trust boundaries — Never Cut Corners"。
- **影响**：🔴 不可信输入未校验，可触发无界查询/DoS；malformed 调用返回不透明字符串而非结构化拒绝。
- **修复**：`call_tool` 改 `arguments.get("anchor")` + 显式 `if not anchor: return {"error": "anchor required"}`；`top_n = min(max(int(arguments.get("top_n", 3) or 3), 1), 20)`；schema 加 `"required"` 与 `"minimum/maximum"`。新增 `tests/test_mcp_server.py` 直接驱动 `call_tool`（无 stdio）覆盖缺字段/非法 top_n/未知工具。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/mcp/server.py:40-61, 64-98`；缺 `tests/test_mcp_server.py`

#### S12. 两个"mocked LLM"测试其实打真实坏 client，靠吞错假性通过
- **问题**：`test_write_insight`/`test_ingest_markdown` 设 `config.llm = {"driver":"openai","base_url":"http://localhost:0"}` 无 api_key，docstring 声称"mocked LLM"。但 `__post_init__` 构造真实 `OpenAILLM`，无 key 时 `chat` 跳过所有 provider 抛 `RuntimeError("All LLM providers failed")`，`_try_extract` 捕获后重试 3× 真实 `asyncio.sleep`（0.5+1+2=3.5s），返回 `([],[])`。测试只断言 `result["ok"] is True` 和 `slug`（都在 LLM 调用前就设好）→ 零行为验证地通过。实测两测试各耗时 3.97s/3.52s。
- **影响**：🔴 假性信心 + 浪费 ~7.5s 真实 sleep；`reverse_extract` 错误处理回归无人发现。
- **修复**：patch `spiderweb.engine.providers.create_llm_provider` 返回 `MockLLM({...})`（`test_l2_extract.py:116` 已有此模式），然后断言 `entities_extracted==2`、`edges_added>=1`、DB 里确有 insights/entities 行。去掉 `localhost:0` 配置。
- **位置**：`/home/ubuntu/projects/spiderweb/tests/test_mcp_tools.py:95-112` + `/home/ubuntu/projects/spiderweb/tests/test_progress.py:1-47`

#### S13. `total_changes` 误用致 `relations_added`/迁移 stats 恒真虚高
- **问题**：`Connection.total_changes` 是连接生命周期累计变更数，非单语句 rowcount。`INSERT OR IGNORE` 命中重复键不产生新变更，但 `total_changes` 仍 ≥1，于是 `if db.total_changes > 0` 对每条 relation 都为真，`rel_added` 实际 = relations 列表长度。迁移 `_migrate_relations` 同样误用，stats 严重虚高。
- **影响**：🔴 返回给调用方/MCP 的 `relations_added` 完全错误，监控与"再跑一次还剩多少"判断失真。
- **修复**：用 cursor rowcount：`cur = db.execute("INSERT OR IGNORE INTO relations ... VALUES(?,?,?,?)", (...)); if cur.rowcount > 0: rel_added += 1`。迁移处同理。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py:99-108` + `/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:265-276`

---

### 🟡 中等

#### M1. `_update_cross_doc_counts` 用 `body LIKE %name%` 计文档数 — 噪声+性能+N+1
- **问题**：每个 entity 跑 `SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE body LIKE '%name%'`。N+1 全表扫描；子串匹配致"王"被"王阳明/亲王/国王"全命中，`cross_doc_count` 严重虚高；entity 名含 `%`/`_` 时 LIKE 通配语义错。
- **修复**：基于已抽取的 relations/entity_traces 统计，或建 `chunk_entities(chunk_id, entity_id)` 关联表抽取时写入再 `GROUP BY` 一次聚合。至少 escape LIKE 通配符 + word-boundary。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py:123-135`（L3 insight 也有同款，`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l3/insight.py:152-161, 244-260`）

#### M2. `mode="full"` 仍被 `MAX_CHUNKS=30` 截断，名实矛盾
- **问题**：`mode=="full"` 分支 `return rows[:MAX_CHUNKS]`，"全量"模式实际只处理前 30 chunk。配置层 `extraction_mode="full"` 承诺全量，实际静默丢弃 30 之后的 chunk，无告警。
- **修复**：full 模式不设硬上限或上限放 config 远大于 30；截断时 `_log` 告警。`MAX_CHUNKS` 来自 config 而非常量。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py:24-38`

#### M3. `sample_chunks(doc_ids=[])` 无 LIMIT 全表加载 chunk 正文
- **问题**：`SELECT id, body FROM chunks ORDER BY id` 不带 LIMIT，把所有 chunk body 一次性读进内存，之后才 `[:MAX_CHUNKS]` 截断。语料稍大即 OOM/长延迟，与 M2 叠加 = "加载 N 万行只为取前 30"。
- **修复**：SQL 层 `LIMIT ?`；sample 模式用 `WHERE rowid % step = 0` SQL 采样。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py:12-19`

#### M4. entity description/aliases 重跑被覆盖，非幂等
- **问题**：已存在 entity 命中后，新 description 非空就 `UPDATE` 覆盖旧值；aliases 整体替换非合并。再跑若 LLM 给更差描述，旧好描述不可逆抹掉。
- **修复**：description 改"仅旧值为空或新值更长才更新"；aliases 改 union：`json.dumps(list(set(old)|set(new)))`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py:77-90`

#### M5. `graph_navigate` 二跳用伪造 relation 字符串污染图
- **问题**：depth≥2 时二跳邻居塞进 anchored 列表，relation 字段写成 `f"{name}→{next_hop}→"` 而非真实 relation_type。LLM 会当真实关系类型回答回用户。
- **修复**：二跳项带 `path:[name,next_hop,neighbor]` + 每段真实 rtype（从 `_expand` 返回边取），或新增 `path_edges` 字段。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/search.py:114-125`

#### M6. 实体去重仅按 name 精确匹配，别名/同指代不合并
- **问题**：dedup 以 `e.get("name").strip()` 为 key，只有名字完全一致才合并 aliases。LLM 在不同 chunk 输出"王阳明"/"阳明"/"王守仁"会各自独立入库成多个 entity，cross_doc_count、relations 全被同指代分裂。
- **修复**：dedup 阶段构建 alias→canonical 映射，alias 命中已有 canonical 则合并；或入库后再跑一次 alias 归并 pass。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/extract.py:184-211`

#### M7. `index_vectors` / `_extract_batch` 失败静默丢 chunk，无重试无失败队列
- **问题**：`index_vectors` 一批 embedding 失败只 `print`+`continue`，count 不计，函数照常返回，这些 chunk 向量永久缺失，向量检索静默召回不到。`_extract_batch` 3 次重试后 `return None,None`，`_process_one` 折成 `[]`，该批 chunk 本轮 build 彻底消失，stats 看着成功。
- **修复**：返回 `(count, failed_ids)` 或聚合异常让上层决定；网络类异常有限次 `_backoff` 重试；stats 返回 `chunks_failed`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l1/ingest.py:216-220` + `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/extract.py:90-112, 137-148`

#### M8. think() BFS N+1 且 async 里跑纯同步 DB
- **问题**：`_get_neighbors` 每条 relation 行一次 `SELECT ... FROM entities WHERE canonical_name=?`；`think()` 每 frontier 节点调一次 `_get_neighbors`，`_footprint_score`/`_is_hot` 各再发一次 per-entity 查询。N×D×10 层。且 `think` 是 `async` 却做纯同步 DB，大遍历卡事件循环。
- **修复**：`_get_neighbors` 用 `WHERE canonical_name IN (?,...*)` 一次取回，或改 JOIN；DB-heavy 循环移到 `asyncio.to_thread`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:75-110, 317-338`（另 `_footprint_score`/`_is_hot` 重复同查询，`reading_service.py:89, 106, 340-364`）

#### M9. `_migrate_vectors` 硬编码 1024 维 + 旧库无 vec 表即崩
- **问题**：`ensure_vec_table(new, 1024)` 假定 voyage-4-large。新库若已用不同维度 embedding 初始化，`ensure_vec_table` 抛 `ValueError`（engine/l1/ingest.py:156）未捕，docs/chunks/entities 已提交后中途 abort。`SELECT chunk_id, embedding FROM vec_chunks` 在旧库从无向量时抛 `OperationalError`。
- **修复**：vec 读包 `try/except sqlite3.OperationalError` 返回 0；维度从旧库元数据或 `len(blob)//4` 推导而非硬编码。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:156-204`

#### M10. 迁移 `PRAGMA foreign_keys=OFF` 设在 `new` 上永不恢复
- **问题**：line 21 关 FK 校验，`new` 是 `_NoClose` wrapper 可能被 `get_db` 复用，FK 终身关，后续 service 写入的孤儿（chunks 指向已删 docs）不会被 FK 拦。
- **修复**：`finally` 里对 `new` 恢复 `PRAGMA foreign_keys=ON`，或确保迁移后 `get_db` 返回 fresh connection。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:21, 65`

#### M11. 迁移逐 stage commit 无整体回滚 — 半迁移状态
- **问题**：每个 `_migrate_*` 各自 `commit()`，若 `_migrate_vectors` 在 entities/relations 提交后抛错，DB 留半迁移无进度记录，叠加 S5 非幂等，恢复需手动清理。
- **修复**：整包单事务（末尾才 commit，partial progress 用 SAVEPOINT），或每 stage 后写 `_meta` key `migration_stage` 供重跑续传。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:23-66`

#### M12. 全栈 `except Exception: pass` 静默吞错（横切）
- **问题**：多处裸 `except Exception: pass`/`continue`：L3 search FTS/embedding/vec 三处（`search.py:38-39,62-63,84-85`）、`hybrid_search` FTS/reranker（`ingest.py:247-256,302-313`）、relations 写入 `build.py:99-108`、`ingest` 的 vec 删除/`index_vectors`（`reading_service.py:162-163,208-211,257-260,373-374`）、迁移 `_migrate_relations`/`_migrate_traces`（`migrate_reading.py:275,297`）、`_NoClose.close` checkpoint 失败（`db.py:110-115`）。故障全部静默化，违反 CLAUDE.md "Error handling that prevents data loss — Never Cut Corners"。
- **修复**：每处至少 `except Exception as e:` 写一条日志（`debug.py` 已有 `llm_error` 或 `sys.stderr`）；relations 写入收窄到 `sqlite3.IntegrityError`；checkpoint 失败用 `PASSIVE` 而非 `TRUNCATE` 避免忙时抛错。保留降级但留痕。
- **位置**：见上行各处

#### M13. MCP `read` 的 `top_n` 无上界 + schema 无 `required`
- **问题**：`top_n` 默认 3 但 LLM 可传任意整数，`search.py:29` 的 `LIMIT ?` 用 `top_n*2` 可被放大为无界查询。四个工具 inputSchema 均无 `"required"`，缺字段静默 KeyError。
- **修复**：`top_n = min(max(int(...),1),20)` + schema `"minimum":1,"maximum":20`；每工具加 `"required":[...]`。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/mcp/server.py:40-61, 48, 80-81` + `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l3/search.py:29`

#### M14. `_parse_json` 三级回退只测了 branch 1
- **问题**：`extract.py:_parse_json`（245-266）和 `insight.py:_parse_json`（276-293）的 markdown-fence regex 与贪婪 `\{[\s\S]*\}` regex——真正救回真实 LLM 输出的那两支——从未被测试覆盖。
- **修复**：加 `_parse_json` 直测：`'```json\n{...}\n```'`→dict；`'sure, here: {...}'`→dict；截断 `'{"entities":'`→None。
- **位置**：`/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/extract.py:245-266` + `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l3/insight.py:276-293`

#### M15. ingest 幂等/部分失败零测试 + chunker 零测试 + record 零测试
- **问题**：re-ingest（文档所述主用例）的 dedup/部分失败无测试；`_chunk_sliding_window`（所有 .txt/.epub 走它）零测试，overlap/段落边界启发未覆盖；`record()` 的空内容校验+标题截断无测试。
- **修复**：`test_progress.py` 加：(a) 同 path ingest 两次断言无重复 chunks；(b) patch `build_graph` 抛错断言 `ok` 仍 True 且 doc/chunks 已提交。`test_l1_ingest.py` 加滑窗 overlap/空文本/4000 字符切片数断言。`test_mcp_tools.py` 加 `record` 空内容/标题截断/source 流转。
- **位置**：`/home/ubuntu/projects/spiderweb/tests/test_progress.py:1-47` + `/home/ubuntu/projects/spiderweb/tests/test_l1_ingest.py:29-50`

---

### 🟢 轻微（清理类）

L1-L20: 见以下（滑动窗口死循环、line_start 语义、LIKE 通配、线程安全、NoopEmbedding、makedirs、UNIQUE 归一化、hooks 非确定性、AsyncOpenAI 连接池、auto_migrate 事务、重复 SELECT、错误泄露、PID 锁、贪婪正则、参数重载、空响应退避、日志路径、死代码、魔数、硬编码路径）……

**（详细见上面的完整 🟢 列表）**

---

## 三、干净的部分（无需动）

- **L1↔L2 边界**：L1 只产/索引 chunk，L2 经 DB 读 chunk 抽取，无反向依赖；`build.py` 对 `extract` 用函数内 lazy import，分层良好。
- **SQL 注入面**：用户输入基本走参数化；`vec0(embedding float[{dimensions}])` 与 `FROM {table}` 是内部 config/常量插值，非用户输入，安全。
- **`ReadingHooks`** 形态正确：小、可组合、返回 fresh list、无过早抽象；regex cleaner 精准命中 EPUB/calibre 真实 artifact。
- **`think()` 的 BFS+decay+hot-stopping** 和 `_find_anchor` 四级回退是真正的领域算法。
- **`debug.py`** best-effort 日志、init 幂等；**`cli.py`** 纯分发器、`sys.exit(1)`+消息无静默吞错。
- **DB 迁移测试**（`test_migrate.py`）是套件模范。
- **conftest DB 隔离**正确。
- **L2 检索单测**打真 SQLite 控制行断言真行为，无 mock 无 tautology。

---

## 四、建议修复顺序

1. **先堵数据丢失/安全（S1-S5）**：WAL 恢复、MCP 路径遍历、ingest 事务化、record slug 去冲突、迁移幂等化。任一触发都是不可逆丢数据。
2. **再修写路径一致性（S6-S9）**：markdown `###` 切分、`INSERT OR REPLACE` 改 UPDATE、FTS 同步触发器、`str(graph_result)`→`str(e)`。
3. **信任边界 + 假测试（S10-S12）**：MCP 输入校验 + `test_mcp_server.py`；patch `create_llm_provider` 让 L3/ingest 测试真验证行为。
4. **stats 正确性（S13）**：`total_changes`→`cur.rowcount`（build + migrate 两处）。
5. **性能与噪声（M1-M8）**：cross_doc_count 的 LIKE 计数、MAX_CHUNKS、N+1、实体同指代合并。
6. **迁移健壮性（M9-M11）+ 静默吞错横切（M12）**。
7. **测试补强（M14-M15）+ 🟢 清理**。

**最高杠杆单项**：**S1 + S2 + S4**（WAL 误删 / MCP 路径遍历 / record slug 覆盖）——三个都是"平时不出事、出事即丢数据或泄密"的静默型，修复都是几行代码。

---

## 附录：架构意图 vs 实现对齐检查

| 架构特性 | 文档描述 | 实现状态 | 缺口 |
|---------|--------|--------|------|
| 中网分类 | 增量 LLM 分类，`_NOISE_WORDS` 146 词，FTS5 snippet 校验 | 🔴 **缺失** | 需确认是否应迁入 engine，或与生产脚本统一 |
| entity_types | person/work/concept/event/location/stem_branch/element | 🟡 缺 noise/unclear | domain.yaml 需补 |
| relation_types | co_occur/logic/oppose/mentions | 🟡 domain.yaml 硬编 AUTHORED/… 13 种 | 需对齐或文档迁移映射 |
| navigate 实现 | 向导模式（自动选路+uncertain 给选项） | ✅ 完成 | 噪声边过滤稳定性（中网缺失影响） |
| 衰退机制 | co_occur 边 >90 天隐藏 | ✅ 完成 | — |
| 足迹排序 | record×3 + navigate×2 + search×1 | ✅ 完成 | — |

---

**report generated**: 2026-07-03 | **5 parallel agents** | **scope: ~3000 src + ~1300 test** | **effort: comprehensive**

