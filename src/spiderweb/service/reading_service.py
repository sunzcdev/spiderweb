"""Reading service — domain adapter for the reading domain.

Exposes four operations (think, read, record, ingest) that encapsulate
the spiderweb L1/L2/L3 engine. Designed to be imported by server.py
and exposed as 4 MCP tools.
"""
import asyncio
import json
import math
from dataclasses import dataclass, field

from spiderweb.engine.domain import DomainConfig
from spiderweb.engine.providers import (
    create_llm_provider, create_embedding_provider,
    create_tokenizer_provider, create_reranker_provider,
    LLMProvider,
)
from spiderweb.engine.l1.ingest import ingest_file, index_vectors, hybrid_search
from spiderweb.engine.l2.build import build_graph
from spiderweb.engine.l2.search import search_entities, graph_stats
from spiderweb.engine.l3.insight import write_insight, sync_insights as _sync_insights


@dataclass
class ReadingService:
    """读书郎的阅读服务 — 封装四个领域操作。

    Usage:
        service = ReadingService(db, config)
        await service.think("妾")
        await service.think("稀缺性", mode="deep")  # 深邃研究
        await service.read("知行合一", source="传习录")
        await service.record("知行合一很重要", source="传习录")
        await service.ingest("/path/to/book.epub")
    """
    db: object
    config: DomainConfig
    _llm: LLMProvider = field(init=False)
    _max_depth: int = field(default=10)
    _hot_record_min: int = field(default=2)
    _hot_navigate_min: int = field(default=5)

    def __post_init__(self):
        self._llm = create_llm_provider(self.config.llm)

    # ═══════════════════════════════════════════════════════════════
    # Public API — think · read · record · ingest
    # ═══════════════════════════════════════════════════════════════

    async def think(self, anchor: str, mode: str = "divergent",
                    max_depth: int | None = None, book: str | None = None) -> dict:
        """探索概念关联网络。两种认知模式：

        - divergent（发散）：横向扫描，发现意外连接。广度优先 + 软主题约束。返回 depths。
        - deep（深邃研究）：纵向深钻，理解结构。收敛 DFS + 硬主题约束。返回 chain。

        Parameters:
            anchor: 概念锚点
            mode: "divergent" | "deep"
            max_depth: 最大跳数（deep 默认 10，divergent 默认 6）
            book: 可选，限定在某本书内思考
        """
        # ── 1. 找锚点 ──────────────────────────────────────────
        entity = self._find_anchor(anchor)
        if entity is None:
            candidates = []
            seen_names = set()
            for e in search_entities(self.db, anchor):
                if e["name"] not in seen_names:
                    seen_names.add(e["name"])
                    candidates.append(e["name"])
            if candidates:
                return {
                    "anchor": anchor, "found": False,
                    "candidates": candidates[:10],
                }
            return {"anchor": anchor, "found": False, "candidates": []}

        # ── 2. 按模式分发 ─────────────────────────────────────
        if mode == "deep":
            return await self._think_deep(entity, max_depth if max_depth is not None else 10, book)
        return await self._think_divergent(entity, max_depth if max_depth is not None else 6, book)

    async def _think_divergent(self, entity: dict, max_depth: int = 6, book: str | None = None) -> dict:
        """发散模式：BFS + 话题相干性约束 + 热点停止。"""
        # 建立话题框架
        context = self._resolve_topic_context(entity["name"], entity["type"], book)

        # 权重归一化
        max_w = self.db.execute("SELECT COALESCE(MAX(weight), 1.0) FROM relations").fetchone()[0]
        max_w = max(max_w, 1.0)

        depths, seen = {}, {entity["name"]}
        frontier = [entity["name"]]
        depth = 1
        threshold = 0.3 if context["doc_ids"] else 0.2

        while frontier and depth <= max_depth:
            all_neighbors = []
            for ent in frontier:
                all_neighbors.extend(self._get_neighbors(ent))

            unique = {}
            for n in all_neighbors:
                if n["name"] in seen:
                    continue
                seen.add(n["name"])

                # 话题相干性过滤
                coherence = self._topic_coherence(n, context, depth)
                if coherence < threshold:
                    continue

                n["coherence"] = round(coherence, 3)
                rel = min(n["weight"] / max_w, 1.0) * (0.5 ** (depth - 1))
                fp = self._footprint_score(n["name"])
                cb = min(n["cross_doc_count"] / 50, 1.0)
                n["_score"] = rel * 0.5 + fp * 0.3 + cb * 0.2
                unique[n["name"]] = n

            if not unique:
                break

            top3 = sorted(unique.values(), key=lambda x: x["_score"], reverse=True)[:3]
            depths[str(depth)] = [
                {"name": n["name"], "type": n["type"],
                 "relation": n["relation"], "in_books": n["cross_doc_count"],
                 "coherence": n["coherence"]}
                for n in top3
            ]

            # 热点停止
            if any(self._is_hot(n["name"]) for n in top3):
                break

            frontier = [n["name"] for n in top3]
            depth += 1

        return {"anchor": entity["name"], "found": True, "depths": depths}

    async def _think_deep(self, entity: dict, max_depth: int = 10, book: str | None = None) -> dict:
        """深邃研究模式：收敛 DFS + 硬主题约束。

        每跳取 coherence 最高的 1 个节点继续，直到没有节点能通过阈值。
        返回一条论证链（不带 rationale 文本，供后续生成）。
        """
        context = self._resolve_topic_context(entity["name"], entity["type"], book)
        threshold = 0.6 if context["doc_ids"] else 0.4

        chain = [{
            "entity": entity["name"],
            "type": entity["type"],
            "depth": 0,
            "coherence": 1.0,
            "relation": None,
        }]
        seen = {entity["name"]}
        frontier = entity["name"]

        for depth in range(1, max_depth + 1):
            neighbors = self._get_neighbors(frontier)

            scored = []
            for n in neighbors:
                if n["name"] in seen:
                    continue
                coherence = self._topic_coherence(n, context, depth)
                if coherence < threshold:
                    continue
                scored.append((coherence, n))

            if not scored:
                return {
                    "anchor": entity["name"],
                    "found": True,
                    "mode": "deep",
                    "chain": chain,
                    "converged_at": depth - 1,
                    "converged_reason": "no_neighbors_above_threshold",
                }

            # 取 coherence 最高的 1 条路径
            scored.sort(key=lambda x: -x[0])
            coherence, best = scored[0]
            seen.add(best["name"])
            chain.append({
                "entity": best["name"],
                "type": best["type"],
                "depth": depth,
                "coherence": round(coherence, 3),
                "relation": best["relation"],
            })
            frontier = best["name"]

        return {
            "anchor": entity["name"],
            "found": True,
            "mode": "deep",
            "chain": chain,
            "converged_at": max_depth,
            "converged_reason": "max_depth_reached",
        }

    async def read(self, query: str, source: str | None = None, top_n: int = 3) -> dict:
        """阅读原文段落。返回完整段落+出处。"""
        if source:
            results = await self._read_in_book(query, source, top_n)
            if not results:
                return {"results": [],
                        "message": f"在《{source}》里没有找到相关内容，试试不限制来源重新搜索"}
            return {"results": results}

        # 全局搜索
        query_vec = await self._embed_query(query)
        tokenizer = create_tokenizer_provider(self.config.tokenizer)
        reranker = create_reranker_provider(self.config.reranker)
        results = await hybrid_search(
            self.db, query, query_vec, top_n, tokenizer, reranker,
            body_max_len=None,
        )
        return {"results": [{"book": r["doc_title"], "section": r["section"], "body": r["body"]}
                            for r in results]}

    async def _read_in_book(self, query: str, book: str, top_n: int) -> list[dict]:
        """在指定书中搜索段落。"""
        doc = self.db.execute(
            "SELECT id FROM docs WHERE title = ?", (book,)
        ).fetchone()
        if not doc:
            return []
        doc_id = doc[0]

        results, seen = [], set()
        tokenizer = create_tokenizer_provider(self.config.tokenizer)

        # FTS5 搜索（限定 doc_id）
        if tokenizer:
            tokens = tokenizer.tokenize(query).split()
            if tokens:
                fts_query = " ".join(f"{t}*" for t in tokens)
                try:
                    rows = self.db.execute(
                        "SELECT c.id, c.section_path, c.body, d.title "
                        "FROM chunks_fts f JOIN chunks c ON f.rowid = c.id "
                        "JOIN docs d ON c.doc_id = d.id "
                        "WHERE chunks_fts MATCH ? AND c.doc_id = ? ORDER BY rank LIMIT ?",
                        (fts_query, doc_id, top_n)
                    ).fetchall()
                    for r in rows:
                        results.append({"book": r[3], "section": r[1], "body": r[2]})
                        seen.add(r[0])
                except Exception:
                    pass

        # LIKE 补充（FTS5 不足时）
        need = top_n - len(results)
        if need > 0:
            rows = self.db.execute(
                "SELECT c.id, c.section_path, c.body, d.title "
                "FROM chunks c JOIN docs d ON c.doc_id = d.id "
                "WHERE c.body LIKE ? AND c.doc_id = ? ORDER BY c.id LIMIT ?",
                (f"%{query}%", doc_id, need)
            ).fetchall()
            for r in rows:
                if r[0] not in seen:
                    results.append({"book": r[3], "section": r[1], "body": r[2]})

        return results[:top_n]

    async def record(self, content: str, source: str | None = None) -> dict:
        """记一条心得。引擎自动抽实体、关联到 L2。"""
        if not content or not content.strip():
            return {"ok": False, "error": "content is empty"}

        title = content[:50].strip().split("\n")[0]
        source_docs = [source] if source else []
        return await write_insight(self.db, self.config, title, content, source_docs)

    async def sync_insights(self) -> dict:
        """Scan insights/*.md, re-index changed/new files into DB.

        Idempotent. Uses file mtime vs DB updated_at to skip unchanged files.
        """
        return await _sync_insights(self.db, self.config)

    async def ingest(self, source: str) -> dict:
        """收录新书。解析、分块、索引、建图，同步等到底。"""
        from spiderweb.engine.db import get_db_path

        file_path = source
        config = self.config
        db = self.db

        # STEP 1: Parse & chunk **FIRST** (no side effects; catches errors early)
        try:
            title, author, chunks = ingest_file(
                file_path,
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
                hooks_module=config.hooks_module,
            )
        except Exception as e:
            return {"ok": False, "error": f"Parse failed: {e}"}

        # STEP 2: Atomic delete-old + insert-new in one transaction
        try:
            db.execute("BEGIN")

            # Check if this path already exists
            existing = db.execute(
                "SELECT id FROM docs WHERE path = ?", (file_path,)
            ).fetchone()

            if existing:
                old_doc_id = existing[0]
                # Delete chunks first (FK: chunks.doc_id → docs.id, no CASCADE)
                db.execute("DELETE FROM chunks WHERE doc_id = ?", (old_doc_id,))
                # Then delete doc (FTS & vec cleanup via triggers on chunks)
                db.execute("DELETE FROM docs WHERE id = ?", (old_doc_id,))

            # Insert new doc
            cursor = db.execute(
                "INSERT INTO docs (path, title, author) VALUES (?, ?, ?)",
                (file_path, title, author)
            )
            doc_id = cursor.lastrowid
        except Exception as e:
            db.rollback()
            return {"ok": False, "error": f"Transaction start failed: {e}"}

        # Insert chunks + FTS5 (triggers auto-sync FTS via external-content mode)
        chunk_ids = []
        tokenizer = create_tokenizer_provider(config.tokenizer)
        try:
            for ch in chunks:
                cid = db.execute(
                    "INSERT INTO chunks (doc_id, section_path, heading_level, body, line_start) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, ch.section_path, ch.heading_level, ch.body, ch.line_start)
                ).lastrowid
                # FTS trigger fires automatically; no need to INSERT into chunks_fts here
                chunk_ids.append(cid)

            db.commit()
        except Exception as e:
            db.rollback()
            return {"ok": False, "error": f"Failed to insert chunks: {e}"}

        # Vector index (sync)
        vec_count = 0
        emb_provider = create_embedding_provider(config.embedding)
        if emb_provider:
            try:
                vec_count = await index_vectors(db, emb_provider, chunk_ids)
            except Exception:
                vec_count = 0

        # Build graph (sync)
        graph_result = {}
        graph_error = None
        try:
            graph_result = await build_graph(db, config, doc_ids=[doc_id])
        except Exception as e:
            graph_error = str(e)

        return {
            "ok": True,
            "doc_id": doc_id,
            "title": title,
            "author": author,
            "chunks": len(chunks),
            "vectors": vec_count,
            "entities_found": graph_result.get("entities_found", 0),
            "relations_added": graph_result.get("relations_added", 0),
            "graph_error": graph_error,  # Surface error if occurred
        }

    # ═══════════════════════════════════════════════════════════════
    # Private helpers
    # ═══════════════════════════════════════════════════════════════

    def _find_anchor(self, anchor: str) -> dict | None:
        """四级降级找锚点：精确→别名完全匹配→分词→CJK 递进前缀。"""
        # 1. 精确匹配 canonical_name
        row = self.db.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE canonical_name = ?",
            (anchor,)
        ).fetchone()
        if row:
            return {"name": row[0], "type": row[1]}

        # 2. 别名完全匹配（anchor 必须完整匹配别名之一）
        for e in search_entities(self.db, anchor):
            aliases = e.get("aliases") or []
            if anchor == e["name"] or anchor in aliases:
                return {"name": e["name"], "type": e["type"]}

        # 3. 分词后的 term 精确匹配
        for term in anchor.split():
            for e in search_entities(self.db, term):
                if term == e["name"] or term in (e.get("aliases") or []):
                    return {"name": e["name"], "type": e["type"]}

        # 4. CJK 递进前缀（"阳明知行合一"→"阳明"）。长度≥2 即可 LIKE 返回。
        for i in range(min(len(anchor), 6), 1, -1):  # i=6..2, cap at 6 to avoid giant loops
            sub = anchor[:i]
            if len(sub) < 2:
                continue
            ents = search_entities(self.db, sub)
            if ents:
                return {"name": ents[0]["name"], "type": ents[0]["type"]}

        return None  # caller 用 search_entities 查候选列表

    def _get_neighbors(self, entity_name: str) -> list[dict]:
        """获取实体的所有邻接节点（含来源文档和描述）。"""
        rows = self.db.execute(
            "SELECT entity_a, entity_b, relation_type, weight "
            "FROM relations WHERE entity_a = ? OR entity_b = ?",
            (entity_name, entity_name)
        ).fetchall()
        neighbors = []
        for a, b, rtype, weight in rows:
            neighbor = b if a == entity_name else a
            row = self.db.execute(
                "SELECT entity_type, cross_doc_count, source_docs_json, description "
                "FROM entities WHERE canonical_name = ?",
                (neighbor,)
            ).fetchone()
            neighbors.append({
                "name": neighbor,
                "type": row[0] if row else "unknown",
                "relation": rtype,
                "weight": weight,
                "cross_doc_count": row[1] if row else 0,
                "source_docs": json.loads(row[2] or "[]") if row and row[2] else [],
                "description": row[3] if row else "",
            })
        return neighbors

    # ── 话题相干性系统 ─────────────────────────────────────────

    def _resolve_topic_context(self, anchor_name: str, anchor_type: str,
                               book: str | None = None) -> dict:
        """建立话题框架：锚点来源文档、根类型、衰减率。

        如果指定了 book，则以该书为唯一语境；否则从锚点的 source_docs_json 推断。
        """
        doc_ids = []
        if book:
            # 指定了书 → 限定在该书内思考
            row = self.db.execute("SELECT id FROM docs WHERE title = ?", (book,)).fetchone()
            if row:
                doc_ids = [row[0]]
        else:
            # 从实体来源文档推断
            row = self.db.execute(
                "SELECT source_docs_json FROM entities WHERE canonical_name = ?",
                (anchor_name,)
            ).fetchone()
            if row and row[0]:
                parsed = json.loads(row[0])
                if parsed:
                    doc_ids = [int(d) for d in parsed]

        return {
            "anchor_name": anchor_name,
            "anchor_type": anchor_type,
            "root_type": anchor_type.split(".")[0] if "." in anchor_type else anchor_type,
            "doc_ids": doc_ids,
            "decay_rate": 2.0,
        }

    def _type_compat(self, anchor_type: str, candidate_type: str) -> float:
        """计算两个实体类型的语义兼容度。

        同完全一致 → 1.0
        同根类型（如 concept.doctrine vs concept） → 0.7
        不同根类型 → 0.2
        """
        if not candidate_type or candidate_type == "unknown":
            return 0.2
        if anchor_type == candidate_type:
            return 1.0
        anchor_root = anchor_type.split(".")[0]
        candidate_root = candidate_type.split(".")[0]
        if candidate_root == "stem_branch" or candidate_root == "element":
            return 0.1  # 干支五行与经济/哲学概念强相关惩罚
        return 0.7 if anchor_root == candidate_root else 0.2

    def _topic_coherence(self, neighbor: dict, context: dict, depth: int) -> float:
        """计算候选实体与话题框架的相干性。

        三信号合成：来源文档重叠 × 0.5 + 类型兼容性 × 0.3 + 深度衰减 × 0.2
        当话题框架没有 doc_ids（无书语境），重新分配权重：
          类型兼容性 × 0.6 + 深度衰减 × 0.4
        """
        doc_ids = context["doc_ids"]

        # 信号 1：来源文档重叠
        doc_overlap = 0.0
        if doc_ids and neighbor.get("source_docs"):
            shared = set(str(d) for d in doc_ids) & set(str(d) for d in neighbor["source_docs"])
            doc_overlap = len(shared) / max(len(doc_ids), 1)

        # 信号 2：类型兼容性
        type_compat = self._type_compat(
            context["anchor_type"],
            neighbor.get("type", "unknown"),
        )

        # 信号 3：深度衰减
        depth_decay = math.exp(-depth / context.get("decay_rate", 2.0))

        if doc_ids:
            # 有明确的书语境 → 全权重（即使 doc_overlap 为 0 也会压低跨书实体）
            return doc_overlap * 0.5 + type_compat * 0.3 + depth_decay * 0.2
        else:
            # 无书语境 → 依赖类型 + 深度
            return type_compat * 0.6 + depth_decay * 0.4

    def _footprint_score(self, entity_name: str) -> float:
        """足迹加权得分。record×3, navigate×2, search×1，归一化到 [0,1]。"""
        rows = self.db.execute(
            "SELECT source, count FROM entity_traces WHERE entity_name = ?",
            (entity_name,)
        ).fetchall()
        weights = {"record": 3, "navigate": 2, "search": 1}
        score = sum(count * weights.get(source, 1) for source, count in rows)
        return min(score / 50, 1.0)  # 50 → 满分

    def _is_hot(self, entity_name: str) -> bool:
        """判断实体是否为'热点'——用户深度关注过的概念。"""
        rows = self.db.execute(
            "SELECT source, count FROM entity_traces WHERE entity_name = ?",
            (entity_name,)
        ).fetchall()
        for source, count in rows:
            if source == "record" and count >= self._hot_record_min:
                return True
            if source == "navigate" and count >= self._hot_navigate_min:
                return True
        # 总加权分 ≥10 也算热点
        weights = {"record": 3, "navigate": 2, "search": 1}
        total = sum(count * weights.get(source, 1) for source, count in rows)
        return total >= 10

    async def _embed_query(self, query: str) -> list[float] | None:
        """Embed a query once. Returns vector or None."""
        emb_provider = create_embedding_provider(self.config.embedding)
        if emb_provider:
            try:
                vecs = await emb_provider.embed([query])
                return vecs[0] if vecs else None
            except Exception:
                pass
        return None
