# reading-graph 修复规划：待办清单 + 依赖关系图

**状态**: 规划中，待执行  
**目标**: 修复 13 个 🔴 严重 + 15 个 🟡 中等问题，使系统达到"数据安全 + 可观测"的基准线  
**总工作量估计**: 30-50 小时代码 + review

---

## 第一阶段：前置准备（4-6 小时）

### 1.1 架构决议与共识
- [ ] **任务**: 与项目负责人确认：
  - spiderweb 是新主干？还是和生产脚本并存？
  - 中网分类是否在本轮合并前纳入？（目前缺失）
  - 中网分类的优先级？（会影响 navigate 质量）
- [ ] **文档**: 更新 CLAUDE.md，记录"data consistency non-negotiables"
- [ ] **工具**: 启用 mypy 在 pre-commit 和 CI

### 1.2 基础知识补充
- [ ] **SQLite WAL 恢复**（15 min 读）: 官方 https://www.sqlite.org/wal.html
- [ ] **FTS5 设计**（15 min 读）: external-content vs embedded 的区别
- [ ] **SQL Trigger**（20 min 写测试）: 为 chunks/chunks_fts 的 sync 写示例
- [ ] **asyncio 并发**（15 min 测试）: to_thread 的用法

### 1.3 测试基础检查
- [ ] **扫描现有测试**: test_migrate.py 是"idempotent 重跑"的范例 ✅ 已看
- [ ] **评估覆盖**: ingest/record/think 的错误路径覆盖度 (估计 <30%)
- [ ] **补充计划**: 哪些新测试必做（下面列出）

**前置完成标志**: 有单独的合并 diff，仅含"基础设施 + 工具"，代码无改动

---

## 第二阶段：基础架构层修复（8-12 小时）

这一层的修复**互相无依赖**，可并行做。

### 2.1 S1 - WAL 恢复启发式 ✅ MUST FIX - P0
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/db.py:132-148`  
**改动**:
1. 删除 `wal_mtime - db_mtime > 120` 的启发式
2. 改为：`os.remove(*-wal/*-shm)` 直接删 → 不删
3. 新增开库后: `PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check`
4. 日志记录 checkpoint 结果

**详细步骤**:
```python
# 旧代码（删除）
if wal_mtime - db_mtime > 120:
    os.remove(f"{db_path}-wal")
    os.remove(f"{db_path}-shm")

# 新代码（替换）
# 删掉上面的代码，让 SQLite 自己 open 时恢复
db = sqlite3.connect(db_path)
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
result = db.execute("PRAGMA integrity_check").fetchone()
assert result[0] == "ok", f"DB integrity check failed: {result}"
```

**测试**:
- [ ] write → ctrl+C before `commit()` → open again → verify all writes intact
- [ ] concurrent processes + WAL → close one → reopen → no data loss

**预计**: 30 min 代码 + 30 min 测试

---

### 2.2 S2 + S11 - MCP 安全边界 ✅ MUST FIX - P0
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/mcp/server.py:40-98`  
**改动**:
1. tool schema 加 `"required"` 和 `"minimum"/"maximum"`
2. `call_tool` 开头做参数校验
3. ingest 的 path 检查白名单
4. top_n 做 clamp

**详细步骤**:
```python
# 步骤 1: 修改 schema
TOOLS = {
    "think": {
        "description": "...",
        "inputSchema": {
            "type": "object",
            "properties": {"anchor": {"type": "string"}},
            "required": ["anchor"],  # 新增
        }
    },
    "read": {
        "inputSchema": {
            "properties": {
                "query": {"type": "string"},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 20}  # 新增 min/max
            },
            "required": ["query"],  # 新增
        }
    },
    # ... others
}

# 步骤 2: call_tool 参数校验
def call_tool(tool_name, arguments):
    # 新增：立即校验
    if tool_name not in TOOLS:
        return {"error": f"Unknown tool: {tool_name}"}
    
    # 必填检查
    required = TOOLS[tool_name]["inputSchema"].get("required", [])
    for field in required:
        if field not in arguments:
            return {"error": f"Missing required parameter: {field}"}
    
    # 类型和值检查
    if tool_name == "read":
        try:
            top_n = int(arguments.get("top_n", 3))
            top_n = max(1, min(top_n, 20))  # clamp
            arguments["top_n"] = top_n
        except (ValueError, TypeError):
            return {"error": "top_n must be an integer"}
    
    # 步骤 3: ingest path 检查（新增）
    if tool_name == "ingest":
        source = arguments.get("source")
        if not source:
            return {"error": "source is required"}
        
        # 白名单检查
        try:
            source_path = Path(source).resolve()
            ingest_root = Path(config.data_dir) / "books"
            ingest_root = ingest_root.resolve()
            
            # 确保在白名单内，且不是符号链接
            if not source_path.is_relative_to(ingest_root):
                return {"error": f"source must be under {ingest_root}"}
            if source_path.is_symlink():
                return {"error": "symlinks not allowed"}
            if not source_path.is_file():
                return {"error": "source must be a regular file"}
            
            arguments["source"] = str(source_path)  # 规范化路径
        except (ValueError, OSError) as e:
            return {"error": f"Invalid path: {e}"}
    
    # ... proceed to actual implementation
```

**测试**:
- [ ] think: missing "anchor" → error with clear message
- [ ] read: top_n=-1 / top_n=10000 / top_n="abc" → clamped safely
- [ ] ingest: source="/etc/passwd" → error (not in whitelist)
- [ ] ingest: source="../../../sensitive" → error (path traversal)

**预计**: 1 小时代码 + 1 小时测试

---

### 2.3 S8 - 错误处理 NameError ✅ MUST FIX - P0
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:262-267`  
**改动**:
```python
# 旧
except Exception:
    graph_result = {"error": str(graph_result)}  # ← graph_result 是 {} 或未定义

# 新
except Exception as e:
    graph_result = {"error": str(e)}
    
# 并在返回 dict 里 surface
return {
    "ok": len(chunks) > 0,
    "entities_found": graph_result.get("entities_found", 0),
    "relations_added": graph_result.get("relations_added", 0),
    "vectors_indexed": vec_count,
    "graph_error": graph_result.get("error"),  # 新增，可能是 None
}
```

**测试**:
- [ ] build_graph raises → result["ok"] 仍 True（docs/chunks ok）；result["graph_error"] 有值

**预计**: 15 min 代码 + 15 min 测试

---

### 2.4 S10 - async 阻塞 I/O ✅ MUST FIX - P0
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/providers.py:101-106, 171-176`  
**改动**:
```python
# 旧
async def embed(self, texts):
    client = voyageai.Client()
    embeddings = client.embed(texts)  # ← blocking，卡 event loop
    return embeddings

# 新
async def embed(self, texts):
    client = voyageai.Client()
    embeddings = await asyncio.to_thread(client.embed, texts)
    return embeddings

# rerank 同理
```

**测试**:
- [ ] concurrent MCP calls 不会互相卡

**预计**: 15 min 代码 + 20 min 测试

---

### 2.5 S13 - total_changes 虚高 ✅ MUST FIX - P0
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l2/build.py:99-108` + `/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:265-276`  
**改动**:
```python
# 旧（build.py）
try:
    db.execute("INSERT OR IGNORE INTO relations...", (a,b,rtype,weight))
    if db.total_changes > 0:  # ← 累计数，总是真
        rel_added += 1
except Exception:
    continue

# 新
try:
    cur = db.execute("INSERT OR IGNORE INTO relations...", (a,b,rtype,weight))
    rel_added += cur.rowcount  # ← 这条语句实际改了多少行
except sqlite3.IntegrityError:
    pass  # expected（UNIQUE 冲突）
except Exception as e:
    log_error(f"Failed to insert relation {a}-{b}: {e}")

# migrate_reading.py 同理
```

**测试**:
- [ ] re-run build_graph with same doc → rel_added = 0（正确）
- [ ] first run with 10 relations → rel_added = 10（正确）

**预计**: 30 min 代码 + 30 min 测试

---

## 第三阶段：数据一致性层修复（12-16 小时）

这一层的修复**有依赖关系**，按顺序做。

### 3.1 S9 - FTS 同步机制 ✅ MUST FIX - P0
**前置**: 理解 FTS5 trigger 模式  
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/db.py:26-29, 72-76`  
**改动**:

策略选择：采用 **external-content 模式** + **trigger** 的组合
- FTS 表定义里加 `content='chunks', content_rowid='id'`
- 每个源表（chunks/insights）创建 AFTER INSERT/UPDATE/DELETE trigger 更新 FTS
- 改所有源表的写操作为"通过存储过程或 helper 函数"（确保 trigger 被触发）

**详细 DDL**:
```sql
-- chunks 表的 trigger（已有的创建语句）
CREATE TABLE chunks(id INTEGER PRIMARY KEY, doc_id TEXT, body TEXT, ...);

-- FTS 表改为 external-content 模式
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    body,
    content='chunks',     -- 新增
    content_rowid='id'    -- 新增，指向 chunks.id
);

-- 新增：trigger 保持 FTS 同步
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES('delete', old.id, old.body);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES('delete', old.id, old.body);
  INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body);
END;

-- insights_fts 同理
```

**代码改动**:
- [ ] 更新 db.py 的 SCHEMA（CREATE TABLE 语句）
- [ ] 为 chunks 和 insights 各创建三个 trigger（INSERT/DELETE/UPDATE）
- [ ] 验证迁移脚本不会破坏 trigger（migrate.py 是否有 DROP TABLE 后再 CREATE）

**测试**:
- [ ] 直接 INSERT chunks → FTS 自动更新 ✅
- [ ] UPDATE chunks.body → FTS 自动更新 ✅
- [ ] DELETE chunks → FTS 对应行消失 ✅
- [ ] search_chunks（FTS 查询）与 chunks 表内容同步 ✅

**预计**: 1 小时 DDL + 1 小时测试 + 1 小时迁移兼容

---

### 3.2 S5 - 迁移幂等化 ✅ MUST FIX - P0
**前置**: S9 完成（否则 FTS 会重复）  
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/migrate_reading.py:108-153, 393-417`  
**改动**:

策略: 采用"整包单事务 + 进度记录"
```python
# 顶层逻辑改为
def auto_migrate(db):
    try:
        db.execute("BEGIN IMMEDIATE")  # 整包事务
        
        # 查进度"我们已经跑到第几 stage 了？"
        current_stage = db.execute(
            "SELECT migration_stage FROM _meta WHERE key='current_migration' LIMIT 1"
        ).fetchone()
        current_stage = current_stage[0] if current_stage else 0
        
        stages = [
            _migrate_docs,
            _migrate_insights,
            _migrate_chunks,
            _migrate_relations,
            _migrate_vectors,
            # ...
        ]
        
        for stage_idx, stage_func in enumerate(stages):
            if stage_idx < current_stage:
                continue  # skip 已完成的 stage
            
            try:
                stats = stage_func(db)
                # 记录进度
                db.execute(
                    "INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
                    ("current_migration", stage_idx + 1)
                )
                db.commit()  # 每 stage 提交
            except Exception as e:
                db.rollback()
                raise RuntimeError(f"Migration stage {stage_idx} failed: {e}")
        
        # 标记完成
        db.execute("INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
                   ("migration_completed", datetime.now().isoformat()))
        db.commit()
        
    except Exception:
        db.rollback()
        raise
```

**各 stage 的改动**:
- `_migrate_chunks`: INSERT 改为 `INSERT OR IGNORE`（需要加 UNIQUE 约束）
- `_migrate_query_history`: INSERT 改为 `INSERT OR IGNORE`（需要 unique index）
- 其他已有 SELECT 判重的 stage：保持不变

**新增 schema 部分**:
```sql
-- _meta 表新增字段来记录迁移进度
CREATE TABLE IF NOT EXISTS _meta(
  key TEXT PRIMARY KEY,
  value TEXT
);

-- 为 chunks 新增 UNIQUE 约束
ALTER TABLE chunks ADD CONSTRAINT chunks_doc_section_unique
  UNIQUE(doc_id, section_path);  -- 如果已有，跳过

-- 为 query_history 新增 unique index
CREATE UNIQUE INDEX IF NOT EXISTS query_history_dedup
  ON query_history(query_text, created_at);
```

**测试**:
- [ ] run migrate() 一次 → count_X = N
- [ ] run migrate() 二次 → count_X = N（不增长）✅
- [ ] 中途 kill 进程 → 重新 run → 从中断处续传 ✅

**预计**: 1.5 小时代码 + 1 小时测试

---

### 3.3 S7 - INSERT OR REPLACE 孤儿化 ✅ MUST FIX - P0
**前置**: S9 完成（FTS 有 trigger）  
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l3/insight.py:203-232`  
**改动**:
```python
# 旧
def write_insight(db, title, content, source):
    slug = title.lower()[:80]
    # INSERT OR REPLACE ← 会改 rowid
    db.execute(
        "INSERT OR REPLACE INTO insights(slug, title, content, source) VALUES(?,?,?,?)",
        (slug, title, content, source)
    )
    insight_id = db.cursor().lastrowid  # ← 新 rowid
    
    # FTS / vec 用新 rowid，旧行变孤儿
    db.execute("INSERT INTO insights_fts(rowid, content) VALUES(?,?)", 
               (insight_id, content))

# 新
def write_insight(db, title, content, source):
    slug = title.lower()[:80]
    
    # 第一步：查询是否已存在
    existing = db.execute(
        "SELECT id FROM insights WHERE slug=?", (slug,)
    ).fetchone()
    
    if existing:
        insight_id = existing[0]
        # UPDATE（保留原 rowid）
        db.execute(
            "UPDATE insights SET title=?, content=?, source=? WHERE slug=?",
            (title, content, source, slug)
        )
        # FTS / vec 用相同 rowid，trigger 自动更新 FTS
        db.execute("UPDATE insights_vec SET embedding=NULL WHERE rowid=?", (insight_id,))
    else:
        # INSERT (新行)
        db.execute(
            "INSERT INTO insights(slug, title, content, source) VALUES(?,?,?,?)",
            (slug, title, content, source)
        )
        insight_id = db.cursor().lastrowid
        # FTS 同样由 trigger 自动处理
    
    # 返回一致的 insight_id
    return insight_id
```

**测试**:
- [ ] record 同 title 两次 → only one insights row ✅
- [ ] FTS 查该 insight → 返回最新内容 ✅
- [ ] vec_insights 指向唯一一条向量 ✅

**预计**: 45 min 代码 + 45 min 测试

---

### 3.4 S4 - record() slug 冲突 ✅ MUST FIX - P0
**前置**: S7 完成（insight 表已改 SELECT→UPDATE）  
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:184-186`  
**改动**:
```python
# 旧
def record(content, source_docs=[]):
    if not content or not content.strip():
        return {"ok": False, ...}
    
    title = content[:50].strip().split("\n")[0]  # ← 截断导致冲突
    
    result = write_insight(db, title, content, source_docs)
    return result

# 新
import hashlib

def record(content, source_docs=[]):
    if not content or not content.strip():
        return {"ok": False, ...}
    
    # title 用于显示，slug 用于唯一标识
    title = content[:50].strip().split("\n")[0]
    
    # slug = title.lower()[:80] + 内容 hash（防冲突）
    content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
    slug = f"{title.lower()[:60]}-{content_hash}"  # 限制 title 部分 60 字，预留空间
    
    result = write_insight(db, slug, title, content, source_docs)
    return result

# 修改 write_insight 的签名
def write_insight(db, slug, title, content, source_docs):
    # slug 现在由上层（record）产生，不在这里生成
    existing = db.execute("SELECT id FROM insights WHERE slug=?", (slug,)).fetchone()
    
    if existing:
        # 已存在 → 返回 error（不覆盖）
        return {
            "ok": False,
            "error": f"insight with slug {slug} already exists",
            "id": existing[0]
        }
    else:
        # 新插入
        db.execute(
            "INSERT INTO insights(slug, title, content, source) VALUES(?,?,?,?)",
            (slug, title, content, source)
        )
        return {"ok": True, "id": cursor.lastrowid}
```

**测试**:
- [ ] record 两条内容不同但前 50 字相同 → both 写入成功，不同 slug ✅
- [ ] record 完全相同内容两次 → 第二条返回 error（已存在）✅

**预计**: 30 min 代码 + 30 min 测试

---

### 3.5 S3 - ingest 先删后插 ✅ MUST FIX - P0
**前置**: S9 (FTS trigger)、S5 (迁移原子) 完成  
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/service/reading_service.py:196-251`  
**改动**:

策略：**parse first，then delete+insert atomically**

```python
def ingest(doc_path):
    # 第一步：解析（无副作用）
    try:
        title, author, chunks = ingest_file(doc_path)
    except Exception as e:
        return {"ok": False, "error": f"parse failed: {e}"}
    
    # 第二步：查询是否已存在
    existing = db.execute("SELECT id FROM docs WHERE path=?", (doc_path,)).fetchone()
    doc_id = existing[0] if existing else None
    
    # 第三步：原子操作（单事务）
    try:
        db.execute("BEGIN")
        
        if doc_id:
            # 删旧（用 doc_id）
            db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            db.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            # FTS 由 trigger 自动清理
        
        # 插新
        db.execute("INSERT INTO docs(path, title, author) VALUES(?,?,?)",
                   (doc_path, title, author))
        doc_id = db.cursor().lastrowid
        
        for chunk in chunks:
            db.execute("INSERT INTO chunks(doc_id, section_path, body, line_start) VALUES(?,?,?,?)",
                       (doc_id, chunk.section_path, chunk.body, chunk.line_start))
            # FTS 由 trigger 自动写入
        
        # 向量化
        embeddings = await index_vectors(db, doc_id)
        
        # 图构建
        graph_result = build_graph(db, doc_id)
        
        # 一切成功，提交
        db.commit()
        
        return {
            "ok": True,
            "doc_id": doc_id,
            "chunks": len(chunks),
            "title": title,
            "entities_found": graph_result.get("entities_found", 0),
            "relations_added": graph_result.get("relations_added", 0),
            "vectors_indexed": embeddings,
            "graph_error": graph_result.get("error"),  # 若有
        }
        
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
```

**测试**:
- [ ] ingest 新书 → docs + chunks + fts 全到位 ✅
- [ ] ingest 既有书的新 version → docs/chunks 被替换，id 一致 ✅
- [ ] ingest 中途失败（如 build_graph raise）→ 旧数据完整（未删）✅

**预计**: 1 小时代码 + 1.5 小时测试

---

## 第四阶段：数据流设计层修复（6-8 小时）

### 4.1 S6 - markdown ### 丢失 ✅ MUST FIX - P0
**文件**: `/home/ubuntu/projects/spiderweb/src/spiderweb/engine/l1/ingest.py:64-94`  
**改动**:
```python
# 旧
def _chunk_markdown(text):
    sections = re.split(r"\n(?=## )", text)  # ← 只分 ##，不分 ###
    chunks = []
    for section in sections:
        # section 包含 ### 子节，全作一个 chunk
        chunks.append(Chunk(...))

# 新
def _chunk_markdown(text):
    # 二级和三级分开处理
    sections = re.split(r"\n(?=##\s)", text)  # ← 改为二级及以上 ##+ 任意数量
    chunks = []
    
    for section in sections:
        # 进一步按 ### 分割子节
        subsections = re.split(r"\n(?=###\s)", section)
        for i, subsection in enumerate(subsections):
            # 标记 heading_level
            heading_level = 3 if i > 0 else 2  # 第一个是 ##，其余是 ###
            section_path = extract_heading(subsection, heading_level)
            
            chunk = Chunk(
                body=subsection,
                section_path=section_path,
                heading_level=heading_level,
                line_start=calculate_line_start(subsection),
            )
            chunks.append(chunk)
    
    return chunks

def extract_heading(text, level):
    """从 text 里提取最顶层的标题作为 section_path"""
    marker = "#" * level
    for line in text.split("\n"):
        if line.startswith(marker + " "):
            return line[len(marker)+1:].strip()
    return "untitled"
```

**测试**:
- [ ] markdown with ## and ### → chunks 按层级分割 ✅
- [ ] only ## → chunks 都是 level 2 ✅
- [ ] ### without ## → error or handle gracefully ✅

**预计**: 45 min 代码 + 45 min 测试

---

### 4.2 S12 - 假 mock LLM 🟡 NICE-TO-HAVE (但建议做)
**文件**: `/home/ubuntu/projects/spiderweb/tests/test_mcp_tools.py:95-112` + `/home/ubuntu/projects/spiderweb/tests/test_progress.py:1-47`  
**改动**:

策略：改 provider factory 支持 MockLLM
```python
# engine/providers.py 新增
class MockLLM(LLMProvider):
    def __init__(self, responses=None):
        self.responses = responses or {
            "extract": "(entities, relations)",
            "chat": "mock response",
        }
    
    async def chat(self, prompt):
        return self.responses.get("chat", "mock")
    
    async def extract(self, prompt):
        return self.responses.get("extract", "([], [])")

# create_llm_provider 改为支持 mock
def create_llm_provider(config):
    driver = config.get("driver")
    
    if driver == "mock":
        return MockLLM(config.get("responses", {}))
    elif driver == "openai":
        return OpenAILLM(config)
    # ...

# 测试改为
def test_write_insight(temp_db):
    config = {"llm": {"driver": "mock"}}
    
    result = write_insight(
        db=temp_db,
        title="test",
        content="test content",
    )
    
    assert result["ok"] is True
    assert result["id"] > 0  # 真实行 id
    
    # 查 DB，验证 row 真的插入了
    row = temp_db.execute("SELECT * FROM insights WHERE id=?", (result["id"],)).fetchone()
    assert row is not None
```

**预计**: 1 小时代码 + 1 小时测试改造

---

## 第五阶段：测试补强 + 文档（4-6 小时）

### 5.1 新增测试 test_mcp_server.py ✅ MUST HAVE
**位置**: `/home/ubuntu/projects/spiderweb/tests/test_mcp_server.py`（新文件）  
**覆盖**:
- [ ] think: missing "anchor" → error with structured message
- [ ] read: top_n invalid values → clamped or error
- [ ] record: empty content → error
- [ ] ingest: path traversal attempted → blocked
- [ ] ingest: symlink → blocked
- [ ] concurrent tool calls → no data corruption

**预计**: 1 小时代码

---

### 5.2 完善 ingest 重跑测试
**位置**: `/home/ubuntu/projects/spiderweb/tests/test_progress.py`  
**新增**:
- [ ] same path ingest twice → chunk count unchanged
- [ ] ingest mid-failure → old doc intact

**预计**: 30 min代码

---

### 5.3 文档更新
- [ ] 更新 README：数据一致性保障说明
- [ ] 更新 CLAUDE.md：add "Error Handling: No Silent Failures" 章节
- [ ] 架构文档：记录触发器设计决策

**预计**: 1 小时

---

## 总体执行顺序（依赖关系图）

```
前置准备 (phase 0)
│ (mypy 启用、知识补充)
└─→ 基础设施层 (phase 1, 可并行)
    ├─ S1: WAL 启发式删除
    ├─ S2: MCP path 校验
    ├─ S8: 错误引用 bug
    ├─ S10: async 阻塞
    ├─ S11: top_n clamp
    └─ S13: total_changes
       
    └─→ 数据一致性层 (phase 2, 严格顺序)
        ├─ S9: FTS trigger [必须第一]
        ├─ S5: 迁移幂等化 [需 S9]
        ├─ S7: INSERT OR REPLACE [需 S9, S5]
        ├─ S4: slug 冲突 [需 S7]
        └─ S3: ingest 事务 [需 S9, S5]
           
           └─→ 数据流 + 测试 (phase 3-4)
               ├─ S6: markdown ###
               ├─ S12: mock LLM
               ├─ 新增测试 (test_mcp_server.py)
               └─ 文档更新
```

**关键路径** (最长的依赖链):
- Phase 0 (前置) → Phase 2 (S9→S5→S7→S4→S3 串行) → Phase 3 (并行)

**估计的关键路径时间**:
- 前置: 4-6 h
- S9 (FTS): 3 h
- S5 (migration): 2.5 h
- S7 (INSERT): 1.5 h
- S4 (slug): 1 h
- S3 (ingest): 2.5 h
- Phase 1 (并行，与 Phase 2 重叠): 4-5 h
- Phase 3 (并行): 3-4 h
- 测试 + 文档: 4-6 h
**总计**: 35-45 h（假设 review + iteration 另算）

---

## 待办清单（copy 给项目管理工具）

### 前置准备
- [ ] 与架构师确认 spiderweb 主干定位
- [ ] 启用 mypy 在 pre-commit 和 CI
- [ ] 学习 SQLite WAL / FTS5 / Trigger / asyncio（各 15-20 min）

### Phase 1: 基础架构（可并行）
- [ ] S1: 删 WAL 启发式 (30 min)
- [ ] S2+S11: MCP 参数校验 (1 h)
- [ ] S8: 错误引用 bug (15 min)
- [ ] S10: asyncio.to_thread (15 min)
- [ ] S13: cursor.rowcount (30 min)
- [ ] Phase 1 测试 (1.5 h)

### Phase 2: 数据一致 (严格顺序)
- [ ] S9: FTS trigger DDL (2 h)
- [ ] S5: 迁移幂等化 (2.5 h)
- [ ] S7: SELECT→UPDATE (1.5 h)
- [ ] S4: slug+hash (1 h)
- [ ] S3: ingest 事务 (2.5 h)
- [ ] Phase 2 测试 (2 h)

### Phase 3: 数据流
- [ ] S6: markdown ### 正则 (1.5 h)
- [ ] S12: mock LLM (2 h)
- [ ] Phase 3 测试 (1 h)

### Phase 4: 测试 + 文档
- [ ] test_mcp_server.py (1 h)
- [ ] 补充 ingest 重跑测试 (0.5 h)
- [ ] 文档更新 (1 h)

---

## 风险和注意事项

| 风险 | 缓解措施 |
|-----|--------|
| FTS trigger 稍微复杂 | 先在开发库测试，实际操作前 backup DB |
| 迁移改动可能遗留垃圾 | 增加 migration test 覆盖_meta 表变化 |
| 事务包装改变并发行为 | profile 并发 ingest，观察锁等待 |
| 已有生产数据的 migration 路径 | 记录旧数据库版本号，写一次性的数据迁移脚本 |

---

## 成功标志

修复完成后：

✅ 13 个 🔴 全修复，相关单测通过  
✅ 迁移脚本可安全重跑  
✅ ingest mid-failure 不会导致数据丢失  
✅ MCP 参数非法时返回结构化错误  
✅ 并发 ingest/record 不会产生孤儿行或冲突   
✅ mypy 通过；无 silent exception  
✅ review report 的 S1-S13 全标记"已修复"  

