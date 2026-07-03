# reading-graph 修复执行摘要

**状态**: 规划已完成，待执行  
**现状**: 13 个 🔴 严重 bug + 15 个 🟡 中等 + 20 个 🟢 轻微问题  
**目标**: 修复所有 🔴，达到"数据安全+可观测"基准线

---

## 核心发现

### 问题本质
- **不是业务 trade-off**：13 个 🔴 全是可设计避免的缺陷或编程错误
- **不是创意受限**：都是"没想到"或"代码审查漏掉"的结果
- **根因三大类**：
  1. **缺失层级/职责不清** (4 个)：WAL 启发式、迁移非幂等、ingest 事务、record 冲突
  2. **数据不变量无保障** (3 个)：INSERT OR REPLACE 孤儿、FTS 无同步触发器、中网分类缺失
  3. **信任边界无校验 + 可观测性失败** (6 个)：MCP 参数、路径遍历、错误吞掉、stats 虚假

### 改之后会达到什么状态
```
当前          →  修复后
────────          ──────────
删 WAL 可丢数据    SQLite 自恢复 ✓
FTS 可 drift      Trigger 自动同期 ✓
重跑迁移有重复    幂等化 + 进度记录 ✓
record 静默覆盖   UUID slug + INSERT error ✓
MPC 无校验        白名单 + 参数检验 ✓
错误被吞          结构化日志 ✓
stats 虚假        cursor.rowcount 准确 ✓
```

---

## 修复优先级与工作量

| 优先级 | 项目 | 工作量 | 依赖关系 | 风险 |
|--------|------|--------|---------|------|
| 🔴 P0 | 基础设施 (S1,S2,S8,S10,S11,S13) | 4-5 h | 无 | 低 |
| 🔴 P0 | 数据一致 (S9→S5→S7→S4→S3 串) | 12-15 h | 链式依赖 | 中（触发器复杂） |
| 🟡 P1 | 数据流 (S6, S12) | 2-3 h | 无 | 低 |
| 🟡 P1 | 测试补强 (test_mcp_server, 重跑测试) | 2-3 h | 无 | 低 |

**总工作量**: 20-26 h 代码 + 4-6 h 测试写入 + review ≈ 40-50 h

---

## 执行步骤（4 个 Phase）

```
┌─ Phase 0: 前置准备 (4-6 h)
│  ├─ 与架构师确认 spiderweb 主干定位 + 中网分类 scope
│  ├─ 启用 mypy 在 pre-commit/CI
│  └─ 学习 SQLite WAL/FTS/Trigger/asyncio
│
├─ Phase 1: 基础设施 (4-5 h, 可并行)
│  ├─ S1: WAL 启发式 → 删掉
│  ├─ S2+S11: MCP 参数校验 → 添加输入检验中间件
│  ├─ S8: 错误引用 bug → except Exception as e
│  ├─ S10: async 阻塞 → asyncio.to_thread
│  └─ S13: total_changes → cursor.rowcount
│
├─ Phase 2: 数据一致 (12-15 h, 严格顺序)
│  │ [关键路径：这链最长]
│  ├─ S9: FTS trigger 📌 [必须第一]
│  ├─ S5: 迁移幂等化 [需 S9]
│  ├─ S7: INSERT→SELECT/UPDATE [需 S9]
│  ├─ S4: slug 冲突 [需 S7]
│  └─ S3: ingest 事务 [需 S9 + S5]
│
└─ Phase 3: 数据流 + 测试 (6-8 h)
   ├─ S6: markdown ### 正则 fix
   ├─ S12: mock LLM 重构
   ├─ test_mcp_server.py (新文件)
   ├─ ingest 重跑测试补强
   └─ 文档更新
```

---

## 关键决策点

| 决策 | 选择 | 理由 |
|-----|------|------|
| **FTS 设计** | external-content + trigger | 保证一致性；trigger 会自动维护 |
| **迁移幂等** | BEGIN + progress_stage tracking | 支持断点续传，半迁移恢复 |
| **slug 去冲突** | title_hash + content_hash | 真正唯一，无碰撞 |
| **ingest 顺序** | parse first, then atomically delete+insert | 解析失败时保留旧数据 |
| **MCP 校验** | decorator pattern（中间件） | 易扩展，易测试 |

---

## 修复后的测试覆盖局部示例

```python
# 数据一致性的关键测试三角
def test_fts_consistency():
    """FTS 与 chunks 表始终同步"""
    insert_chunk(1, "hello world")
    assert search_fts("hello") == [1]  # FTS 有数据
    
    delete_chunk(1)
    assert search_fts("hello") == []  # FTS 自动清理
    
    update_chunk(1, "goodbye")
    assert search_fts("hello") == []
    assert search_fts("goodbye") == [1]

def test_ingest_atomicity():
    """ingest 中途失败，旧数据毫发未损"""
    ingest("book1.epub")
    old_count = count_chunks()
    
    # 模拟 build_graph 失败
    with patch('build_graph', side_effect=RuntimeError):
        result = ingest("book1.epub")  # 重新入库
    
    assert result["ok"] is False  # 操作失败
    assert count_chunks() == old_count  # 旧数据完整
    
    # 再跑一次成功
    result = ingest("book1.epub")
    assert result["ok"] is True

def test_migrate_idempotent():
    """迁移脚本可安全重跑"""
    migrate()
    stats1 = get_stats()
    
    migrate()
    stats2 = get_stats()
    
    assert stats1 == stats2  # 完全相同
```

---

## 风险和缓解

| 风险 | 缓解措施 |
|-----|--------|
| **FTS trigger 稍复杂** | 先在测试库验证；backup 生产 DB；跑 integrity_check |
| **迁移改动可能遗留垃圾** | _meta 表变化完全覆盖测试；制作回滚脚本 |
| **事务化改变并发行为** | 新增 concurrency profile test；观察锁等待时间 |
| **生产数据迁移路径** | 版本号 tracking；write one-time migration script separately |

---

## 审查检查清单

修复完成后，用这个清单 verify：

### S1-S5（基础 + 早期数据一致）
- [ ] WAL 恢复：open DB → wal_checkpoint + integrity_check ✓
- [ ] MCP 参数：缺字段/非法値 → 结构化错误，不吞掉 ✓
- [ ] 错误处理：所有 exception 用 `as e` capture ✓
- [ ] async：无 blocking I/O 在 event loop ✓
- [ ] stats：rel_added/chunks_indexed 用 cursor.rowcount ✓

### S6-S9（关键数据路径）
- [ ] FTS：INSERT chunks → FTS 自动更新 ✓
- [ ] 迁移：run migrate() 两次 → 输出完全相同 ✓
- [ ] INSERT OR REPLACE：record 同 title 两次 → 一条行，无孤儿 ✓
- [ ] slug：UUID hash，无冲突可能 ✓

### S10-S13（整体一致性）
- [ ] ingest：中途失败（force build_graph raise）→ 旧数据完整 ✓
- [ ] markdown：## 和 ### 子节分别 chunk ✓
- [ ] MockLLM：test 用 mock driver，无实际网络调用 ✓
- [ ] 并发：多进程 ingest/record → 无数据损坏 ✓

---

## Review 前的问卷

执行前，问自己：

1. **职权范围**：哪些改动需要架构师 sign-off？（FTS trigger 设计、迁移 scope）
2. **时间估计**：是否争取本 sprint 完成？还是跨 sprint？
3. **测试要求**：新增测试是否 cover 所有 failure scenarios？
4. **生产数据**：旧的 DB 如何迁移到新 schema？单独脚本还是自动识别？
5. **回滚计划**：若新代码有 bug，怎么快速回滚？

---

## 下一步

1. **即刻做**：
   - 读 ARCHITECTURE-ANALYSIS.md（30 min）
   - 读 TODO-AND-PLAN.md 的 Phase 0 + Phase 1（1 h）

2. **本周做**：
   - 与架构师对齐（s.2, 中网分类 scope）
   - 启用 mypy（15 min）
   - 快速原型 Phase 1（2-3 h，不阻塞 review）

3. **开启 PR**：
   - PR 1: Phase 0 + Phase 1（基础层，无依赖）→ 快速 merge
   - PR 2: Phase 2（数据一致，大 diff 但可讲清故事）
   - PR 3: Phase 3（数据流 + 测试）
   - PR 4: 文档（汇总）

4. **交付**：
   - [ ] 所有 🔴 标记"已修复"
   - [ ] code-review report 更新版本
   - [ ] 迁移指南（生产数据从旧→新 schema）
   - [ ] 运维 runbook（排查数据一致性的命令）

---

## 关键文件导航

| 文件 | 用途 | 阅读时间 |
|-----|------|----------|
| `CODE-REVIEW-REPORT-2026-07-03.md` | 完整的 review 发现 | 30 min |
| `ARCHITECTURE-ANALYSIS.md` | 根本原因 + 设计反思 | 45 min |
| `TODO-AND-PLAN.md` | 逐步修复计划（详细） | 1.5 h |
| `EXECUTIVE-SUMMARY.md` | 本文，5 min 快速了解 | 5 min |

---

**Last update**: 2026-07-03  
**Status**: Ready for execution

