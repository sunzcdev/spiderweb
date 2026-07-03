# reading-graph 架构设计分析与 Bug 根本原因诊断

**日期**: 2026-07-03  
**目的**: 从架构设计层面分析 13 个 🔴 严重问题是否可避免，确定哪些是 bug vs trade-off，规划修复依赖关系  
**前置共识**: spiderweb 是新主干，应完整迁入所有特性（包括中网分类）

---

## 第一部分：根本原因诊断（13 个 🔴 分类）

### 分类 A：**缺失层级 / 职责边界不清**（可设计避免型）

#### A1. S1 - WAL 恢复启发式（db.py:132-148）
```
现象: wal_mtime - db_mtime > 120 时直接删 *-wal/*-shm
根本原因: 自研启发式试图"清理过期 WAL"，但误解了 SQLite WAL 的语义
        → WAL 里的数据是结构化的（transient，不是 stale），不应按时间启发删除
可避免性: ✅ 完全可避免 
设计缺陷: 没有信任 SQLite 的自恢复能力，觉得自己更聪明
建议改为: 删除启发式；信任 SQLite open 时的恢复；开库后 PRAGMA wal_checkpoint(TRUNCATE) + integrity_check
trade-off: 无。删启发式反而更简洁。
```

#### A2. S5 - 迁移非幂等（migrate_reading.py:108-153, 393-417）
```
现象: _migrate_chunks / _migrate_query_history 无条件 INSERT，重跑产生重复行
根本原因: 迁移脚本设计为"一次性"，没有幂等性作为**基本假设**
        → 数据库演变脚本应该总是幂等的，因为"安全的重跑"是运维基本需求
可避免性: ✅ 完全可避免
设计缺陷: 没有在 schema 设计时建立"唯一性约束"，迁移时无法利用 INSERT OR IGNORE
建议改为: (1) 每个 _migrate_* 都需要 SELECT 判重或 INSERT OR IGNORE
         (2) 写 _meta 记录 migration_version，next run 时 skip
         (3) 整包单事务以及 SAVEPOINT 支持 partial rollback
trade-off: 无。这是基本的运维要求。
```

#### A3. S3 - ingest 先删后导（reading_service.py:196-251）
```
现象: 先删旧 docs/chunks/fts/vec，再解析新文件、插入；解析失败 → 旧数据已删
根本原因: 没有把"删"和"插"作为**原子操作**来设计
        → 业务逻辑把这两个操作分在事务的两端，中间有"空白窗口"
可避免性: ✅ 完全可避免
设计缺陷: 没有"解析-验证-再操作"的 pipeline；操作顺序错
建议改为: (1) 先解析+验证新文件 → 得到 (title, author, chunks)
         (2) 再在一个事务里删旧+插新
         (3) 异常发生在 (1) 里时，旧数据毫发未损
trade-off: 无。这是正确的操作顺序。
```

### 分类 B：**数据不变量无机制保障**（设计层缺少约束）

#### B1. S4 - record() slug 冲突（reading_service.py:184-186 + insight.py:203-232）
```
现象: 两条内容前 50 字相同的 record，后者静默覆盖前者（INSERT OR REPLACE）
根本原因: slug 是充要唯一标识，但 title 的生成方式（50 字截断）会冲突
        → 没有为"唯一性"设计建立防冲突机制
可避免性: ✅ 完全可避免
设计缺陷: (1) title 不应该和 slug 绑定用作唯一键
         (2) 应该用 UUIDv4 或内容 hash 作为 slug（或用自增 ID）
         (3) 不应该用 INSERT OR REPLACE 这样的"静默替换"
建议改为: (1) slug = f"{title-hash}-{crc32(content)}"（或用 nanoid）
         (2) 用 INSERT，冲突时返回 error 给 LLM/user
         (3) 或用自增 ID 作 pk，slug 从唯一转为可选展示字段
trade-off: 无。这是基本的数据库设计原则。
```

#### B2. S7 - INSERT OR REPLACE 孤儿化（insight.py:203-232）
```
现象: INSERT OR REPLACE 删旧行获新 rowid，但 FTS/vec 表用 lastrowid 写新行
      → 旧行的 FTS/vec 变孤儿（rowid 指向已删行）
根本原因: 没有理解 INSERT OR REPLACE 的行为（删+再插 → 新 rowid）
        → 关联表的一致性无"触发器"或"原子操作"保障
可避免性: ✅ 完全可避免
设计缺陷: (1) 不应该用 INSERT OR REPLACE（这本身就容易出错）
         (2) 应该用 SELECT→UPDATE or INSERT 的显式逻辑
         (3) 或在 schema 层用 trigger 自动同步 FTS/vec
建议改为: (1) 改为 SELECT id; 存在→UPDATE; 不存在→INSERT
         (2) 保留原 rowid，后续 FTS/vec 用该 id（不用 lastrowid）
         (3) 或在 schema 里每个表都定义 AFTER INSERT/UPDATE/DELETE trigger
trade-off: 无。INSERT OR REPLACE 本身就是反 pattern。
```

#### B3. S9 - FTS5 无同步触发器（db.py:26-29, 72-76）
```
现象: chunks 表和 chunks_fts 独立；任何不经 FTS 表的 chunks 修改/删除都掉队
根本原因: 没有在 DDL 设计环节建立"FTS 与源表的同步机制"
        → schema 只创建了两个独立的表，没有触发器
可避免性: ✅ 完全可避免
设计缺陷: SQLite FTS5 有两种模式：
         (1) external-content 模式：FTS 指向源表，自己不存数据，需要 trigger
         (2) embedded 模式：FTS 自己存数据
         此处选用了最复杂的模式（embedded？还是 external？），但没防护
建议改为: (1) 如果用 external-content：在 DDL 里为 chunks 定义 AFTER INSERT/UPDATE/DELETE trigger
         (2) 如果改为 embedded：收敛所有 chunk 写入到单个 insert_chunk() helper
         (3) 或采用"FTS 参与所有写操作"的编码规范
trade-off: 无。这是 SQLite + FTS 的基本最佳实践。
```

### 分类 C：**信任边界无校验 + 类型检查缺失**（安全/防御设计）

#### C1. S2 - MCP ingest 路径遍历（mcp/server.py:56-60）
```
现象: arguments["source"] 直接当文件路径用，LLM 可传 /etc/passwd 或 ../../../
根本原因: 没有在**信任边界**（MCP tool input）建立校验机制
        → 假定上游输入是安全的，但 LLM 本质上是不可信的
可避免性: ✅ 完全可避免
设计缺陷: 违反 CLAUDE.md 的"Input validation at trust boundaries — Never Cut Corners"
        应该在 MCP 入口做**严格的参数校验**
建议改为: (1) call_tool 接收 arguments 后，**第一步** do sanitization
         (2) source path → Path(source).resolve() → assert is_relative_to(ingest_root)
         (3) config 里定义 ingest_dirs 白名单
trade-off: 无。安全是必做。
```

#### C2. S11 - MCP 参数无校验 + top_n 无界（mcp/server.py:40-98）
```
现象: (1) arguments["anchor"] 裸索引，缺字段→KeyError→吞掉→模糊错误
      (2) top_n 无上界，LLM 传 10**9 → 无界查询 → DoS
根本原因: (1) 没有 schema 里定义 required 字段
         (2) 没有对参数值做范围校验
         (3) MCP server 本身缺少**输入检验中间件**的模式
可避免性: ✅ 完全可避免
设计缺陷: 没有在架构层设计"MCP 入口验证 pipeline"
建议改为: (1) tool schema 里加 "required": ["anchor"] 等
         (2) call_tool 里对每个 param 做显式校验（type check, range check）
         (3) 定义全局 MCP_PARAM_VALIDATOR 或 middleware 模式
trade-off: 无。安全是必做。
```

### 分类 D：**错误处理沦为静默失败**（可观测性）

#### D1. S8 - 错误信息变量引用 bug（reading_service.py:262-267）
```
现象: except Exception: 块里 str(graph_result)，但 graph_result 刚被赋成 {}
根本原因: 赤裸的**编程错误**，在代码审查中应该被发现
可避免性: ✅ 只需代码审查就可避免
设计缺陷: 没有**静态类型检查**（这个 bug 会被 mypy 发现）
建议改为: (1) 用 except Exception as e: 而非 except Exception:
         (2) 启用 mypy 或 pyright 做类型检查
         (3) 建立代码审查的"exception handling 检查清单"
trade-off: 无。这是基本的错误处理规范。
```

#### D2. S12 - 测试用假 LLM 其实打真异常（tests/test_mcp_tools.py:95-112）
```
现象: config.llm={"driver":"openai","base_url":"http://localhost:0"} 被当"mocked LLM"
      但实际打真实 OpenAI client → 失败 → 3 次 sleep 重试 → 秒表测不出
根本原因: 没有建立"mock provider"的公共基础设施
        → 每个测试都得自己 patch，容易出错
可避免性: ✅ 可避免，但需要重构 provider 工厂
设计缺陷: `create_llm_provider` 函数没有考虑"测试模式"
        应该支持 MockLLM 作为一类 driver
建议改为: (1) 定义 MockLLM class（已在 test_l2_extract.py:116 存在）
         (2) 让 create_llm_provider 支持 "driver":"mock" → MockLLM
         (3) 或在 provider config 层有 env var 覆盖
trade-off: 无。这是测试架构应该支持的。
```

#### D3. S13 - total_changes 恒真虚高（build.py:99-108, migrate_reading.py:265-276）
```
现象: if db.total_changes > 0: count += 1，但 total_changes 是连接生命周期累计数
      结果 rel_added stats 虚高
根本原因: 不了解 sqlite3.Connection.total_changes 的语义
       这不是 SQL 层的 rowcount，而是连接级别的统计
可避免性: ✅ 完全可避免（只需用正确的 API）
设计缺陷: 没有了解并正确使用 DB API 的语义
建议改为: (1) 用 cursor.rowcount（这是 SQL 语句的真实行数）
         (2) 或 cursor.execute(...); cursor.rowcount 后立即检查
         (3) 定义 build_stats 数据类追踪 (new, updated, deleted, failed) 四个数字
trade-off: 无。这是基本的 DB API 使用。
```

### 分类 E：**数据流设计缺陷**（pipeline 没考虑失败路径）

#### E1. S6 - markdown 子节被吞（ingest.py:64-94）
```
现象: _chunk_markdown regex lookahead 只匹配 ## ，不匹配 ### 
      → ### 子节全部并进父节 chunk
根本原因: regex 写得不对（r"\n(?=## )" 不会匹配 ###+）
        这是最简单的 bug 类型：copy-paste or misunderstanding regex
可避免性: ✅ 完全可避免（只需正确写 regex 或用递归分割）
设计缺陷: 没有充分的单元测试（_chunk_markdown 的测试只覆盖简单情形）
建议改为: (1) 改 regex 为 r"\n(?=##\s)"（二级及以上）
         (2) 或二次遍历：先按 ## 分，再按 ### 分
         (3) 或改用递归算法逐级处理标题层级
trade-off: 无。这是正确的实现。
```

#### E2. S10 - async 里跑阻塞 I/O（providers.py:101-106）
```
现象: VoyageEmbedding.embed() 是 async def 但调 voyageai.Client().embed() 同步
      在事件循环线程上 block，卡住其他协程
根本原因: 没有理解 async 的约束（不能在 event loop 线程上做 blocking I/O）
        或者当时没有 async voyage client，只能这样
可避免性: ~部分可避免~ 取决于依赖库的情况
设计缺陷: (1) 没有用 asyncio.to_thread() 把 blocking call 移出 event loop
         (2) 或者应该在架构设计时就谋划"所有 I/O 必须 async"
建议改为: (1) await asyncio.to_thread(voyageai.Client().embed, ...)
         (2) 或找/开发 async voyage client
trade-off: 小。这个修复就几行。
```

---

## 第二部分：设计级反思 - 从零开始怎么避免这些问题？

### 如果重新设计 L1→L2→L3→Service 这个 pipeline...

#### 核心设计原则

| 原则 | 现状缺陷 | 修复设计 |
|-----|--------|--------|
| **数据不变量** | FTS/vec/chunks 可 drift；INSERT OR REPLACE 致孤儿 | SQL Trigger + external-content FTS；UUIDv4 pk；避免 INSERT OR REPLACE |
| **事务边界** | 先删后插；迁移无原子性 | 解析完毕再操作；整事务包装；SAVEPOINT 支持 partial rollback |
| **幂等性** | 迁移可重跑导致重复行 | 每个写操作都应该能支持 idempotent；schema 层建立唯一性约束 |
| **信任边界** | MCP 无参数校验；LLM 可注入 | 入口做严格参数检验中间件；path 必须在白名单内；type check + range check |
| **可观测性** | 故障静默化；stats 虚假 | 所有异常都记日志；分清"可恢复降级"vs"真故障"；metrics 用真实 API |
| **类型安全** | 没有静态检查；异常处理混乱 | mypy/pyright；exception 用 `as e`；定义自己的 Exception 层级 |
| **Pipeline 逻辑** | 不同 chunker 的 line_start 语义混乱 | 统一的数据流接口，enforce 一致性 |

#### 理想的 L1→L2→L3 Pipeline 设计

```
┌─ L1: ingest
│  ├─ parse_file(path) → (metadata, raw_text)  # 纯 I/O + 解析，无副作用
│  ├─ chunk_text(raw_text) → Chunks[]  # 纯计算
│  ├─ [验证] 如果成功
│  └─ _persist(metadata, chunks)  # 原子：两个表同步写
│
├─ L2: build_graph
│  ├─ extract_entities(chunks) → Entities[], Relations[]  # LLM 抽取，可失败可重试
│  ├─ dedup_entities(Entities) → canonical Entities[]  # 按 name 去重
│  ├─ classify_entities(Entities) → Entities[].entity_type  # 可选：中网分类
│  ├─ [验证] 如果成功
│  └─ [写 DB]
│       ├─ 删旧（用 doc_id）
│       ├─ 插新
│       └─ 原子提交
│
├─ L3: record_insight
│  ├─ parse_llm_output(text) → (entities, relations)  # JSON 解析，三级回退
│  ├─ link_insight_to_entities()  # 更新关系
│  ├─ upsert_insight(slug, content)  # SELECT→UPDATE or INSERT
│  └─ 原子性保障在行级（一个 insight 一个事务）
│
└─ Service: 编排（think/read/record）
   ├─ 验证输入（path、query、content）
   ├─ 调用各层
   ├─ 错误分类（transient？permanent？）
   └─ 返回结构化结果
```

#### 数据一致性 - 三级防御

| 防御层 | 现状 | 改为 |
|--------|------|------|
| **Schema 约束** | 缺少 trigger；no unique 约束 | TRIGGER for FTS sync；CHECK 约束；FK on |
| **事务原子性** | 混乱的 commit() 散落各处 | 明确的事务边界；SAVEPOINT 支持 partial |
| **应用层检查** | 数据一致性靠"希望"和注释 | 编程规范：所有写操作通过 DAO 层；定期 integrity_check |

---

## 第三部分：问题分类矩阵（必须改 vs 可接受 vs nice-to-have）

| ID | 问题 | 可避免性 | Trade-off | 优先级 | 改之前需要 |
|----|------|--------|----------|-------|-----------|
| S1 | WAL 启发式 | ✅ 完全 | 无 | 🔴P0 | 理解 SQLite WAL |
| S2 | MCP 路径遍历 | ✅ 完全 | 无 | 🔴P0 | 安全审查 checklist |
| S3 | ingest 先删后插 | ✅ 完全 | 无 | 🔴P0 | 重构 ingest pipeline |
| S4 | record slug 冲突 | ✅ 完全 | 无 | 🔴P0 | 重新设计 slug |
| S5 | 迁移非幂等 | ✅ 完全 | 无 | 🔴P0 | 增加 idempotent check |
| S6 | markdown ### 丢失 | ✅ 完全 | 无 | 🔴P0 | fix regex + 单测 |
| S7 | INSERT OR REPLACE 孤儿 | ✅ 完全 | 无 | 🔴P0 | 改为 SELECT→UPDATE/INSERT |
| S8 | 错误引用 NameError | ✅ 完全 | 无 | 🔴P0 | 启用 mypy |
| S9 | FTS 无同步 | ✅ 完全 | 无 | 🔴P0 | DDL + trigger 设计 |
| S10 | async 阻塞 I/O | ~可部分避免 | 微 | 🟡P1 | asyncio.to_thread |
| S11 | MCP 参数无校验 | ✅ 完全 | 无 | 🔴P0 | 参数校验中间件 |
| S12 | 假 mock LLM | ~可避免 | 微 | 🟡P1 | 重构 provider factory |
| S13 | total_changes 虚高 | ✅ 完全 | 无 | 🔴P0 | 用 cursor.rowcount |

**结论**:
- **13 个 🔴 全是可避免的设计缺陷或 bug，不是业务 trade-off**
- **无一是"创意受限"或"工程实现困难"**
- **都是"没想到" + "代码审查不够仔细"的结果**

---

## 第四部分：问题依赖关系图

```
基础架构涂层
├─ S1 (WAL 启发式) ← 无依赖，独立修
├─ S2 (MCP 路径) ← 无依赖（安全修补）
├─ S11 (MCP 校验) ← 依赖 S2（一起评审）
└─ S8 (错误引用) ← 建议先启用 mypy

数据一致性层
├─ S9 (FTS 同步) ← 依赖 schema 设计理解
├─ S5 (迁移幂等) ← 需要 S9 的基础（都在 db 层）
├─ S3 (ingest 事务) ← 需要 S9 (FTS trigger 处理)
├─ S4 (slug 冲突) ← 独立修（L3 层）
└─ S7 (INSERT OR REPLACE 孤儿) ← 与 S9 相关（FTS 设计）

数据流设计层
├─ S6 (markdown ### 丢失) ← 独立修（L1 chunker）
├─ S10 (async 阻塞) ← 需要异步框架理解
├─ S13 (total_changes) ← 两处要改（build.py + migrate.py）
└─ S12 (mock LLM) ← 需要先理解 provider 架构

关键依赖关系（构建顺序）:
  phase 1(基础 + 安全):  S1, S2, S8, S11
  phase 2(数据一致):    S9, S5, S7, S13  # 这些都涉及 DB 改动
  phase 3(数据流):      S3, S4, S6, S10, S12  # 这些是业务逻辑
  phase 4(测试重构):     S12 + M15 (补充测试)
```

---

## 第五部分：修复前的前置工作

### 必做

1. **架构决议** 
   - [ ] 确认 spiderweb 是主干，中网分类要迁入
   - [ ] 定中网分类的 scope（是否本次合并前）

2. **理解基础**
   - [ ] SQLite WAL 恢复机制（官方文档 10 分钟）
   - [ ] FTS5 external-content vs embedded 模式（15 分钟）
   - [ ] SQL Trigger 语法（15 分钟）
   - [ ] asyncio 并发模式（20 分钟）

3. **工具准备**
   - [ ] 启用 mypy 或 pyright（在 CI 里）
   - [ ] 增加 pre-commit hook 做类型检查

4. **测试基础**
   - [ ] 确保 `test_migrate.py` 测试复用（migrate 是 idempotent 的范例）
   - [ ] 为 ingest 流程补充"重复调用不重复 chunk"的测试

### 组织准备

- [ ] 主要贡献者对 13 个问题达成一致（bug vs trade-off）
- [ ] 约定"提交大小"：每个原子修改 ≤100 行（Martin Fowler 建议）

---

## 附录：改之后的架构检查清单

修复完这 13 个 🔴 后，系统应该具备：

- [ ] **数据完整性**: WAL/FTS/vec 不会丢失；并发写下不会产生孤儿行
- [ ] **事务原子性**: L1/L2/L3 的写操作都是 all-or-nothing；迁移支持幂等重跑
- [ ] **错误可观测**: 没有"错误被吞掉"；stats 和真实数据一致
- [ ] **安全边界**: 所有 untrusted input（MCP 参数、文件路径）都有校验
- [ ] **性能**: async 不会被 blocking I/O 卡；query 没有 N+1；FTS 维护自动化

