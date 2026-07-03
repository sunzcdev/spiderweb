"""Reading service — domain adapter for the reading domain.

Exposes four operations (think, read, record, ingest) that encapsulate
the spiderweb L1/L2/L3 engine. Designed to be imported by server.py
and exposed as 4 MCP tools.
"""
import asyncio
from dataclasses import dataclass, field

from spiderweb.engine.domain import DomainConfig
from spiderweb.engine.providers import (
    create_llm_provider, create_embedding_provider,
    create_tokenizer_provider, create_reranker_provider,
    LLMProvider,
)
from spiderweb.engine.l1.ingest import ingest_file, index_vectors, hybrid_search, ensure_vec_table
from spiderweb.engine.l2.build import build_graph
from spiderweb.engine.l2.search import search_entities, graph_stats
from spiderweb.engine.l3.insight import write_insight


@dataclass
class ReadingService:
    """读书郎的阅读服务 — 封装四个领域操作。

    Usage:
        service = ReadingService(db, config)
        await service.think("妾")
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

    async def think(self, anchor: str) -> dict:
        """探索概念关联网络。锚点→深度展开→热点停止。"""
        # ── 1. 找锚点 ──────────────────────────────────────────
        entity = self._find_anchor(anchor)
        if entity is None:
            # 搜候选：只返回名字包含锚点 or 别名包含锚点的（比 search_entities 的 LIKE 更收敛）
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

        # ── 2. 深度展开 ─────────────────────────────────────────
        # 预加载所有关系的权重范围（用于归一化）
        max_w = self.db.execute("SELECT COALESCE(MAX(weight), 1.0) FROM relations").fetchone()[0]
        max_w = max(max_w, 1.0)

        depths, seen = {}, {entity["name"]}
        frontier = [entity["name"]]  # 当前层的节点
        depth = 1

        while frontier and depth <= self._max_depth:
            # 收集当前层所有邻居
            all_neighbors = []
            for ent in frontier:
                all_neighbors.extend(self._get_neighbors(ent))

            # 去重
            unique = {}
            for n in all_neighbors:
                if n["name"] in seen:
                    continue
                seen.add(n["name"])
                # 锚点关系强度 = 归一化权重，随深度衰减
                rel = min(n["weight"] / max_w, 1.0) * (0.5 ** (depth - 1))
                fp = self._footprint_score(n["name"])
                cb = min(n["cross_doc_count"] / 50, 1.0)
                n["_score"] = rel * 0.5 + fp * 0.3 + cb * 0.2
                unique[n["name"]] = n

            if not unique:
                break

            # 取 top 3
            top3 = sorted(unique.values(), key=lambda x: x["_score"], reverse=True)[:3]
            depths[str(depth)] = [
                {"name": n["name"], "type": n["type"],
                 "relation": n["relation"], "in_books": n["cross_doc_count"]}
                for n in top3
            ]

            # 检查热点：任一 top3 节点是"熟路"即可停
            if any(self._is_hot(n["name"]) for n in top3):
                break

            frontier = [n["name"] for n in top3]
            depth += 1

        return {"anchor": entity["name"], "found": True, "depths": depths}

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

        # Generate unique title+slug to avoid collisions
        title = content[:50].strip().split("\n")[0]

        # Append content hash to slug to prevent collisions
        import hashlib
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        slug_base = title.lower()[:60]  # Leave room for hash
        title_with_hash = f"{title} #{content_hash}"  # Display-friendly version

        source_docs = [source] if source else []
        return await write_insight(self.db, self.config, title_with_hash, content, source_docs)

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
        """获取实体的所有邻接节点。"""
        rows = self.db.execute(
            "SELECT entity_a, entity_b, relation_type, weight "
            "FROM relations WHERE entity_a = ? OR entity_b = ?",
            (entity_name, entity_name)
        ).fetchall()
        neighbors = []
        for a, b, rtype, weight in rows:
            neighbor = b if a == entity_name else a
            row = self.db.execute(
                "SELECT entity_type, cross_doc_count FROM entities WHERE canonical_name = ?",
                (neighbor,)
            ).fetchone()
            neighbors.append({
                "name": neighbor,
                "type": row[0] if row else "unknown",
                "relation": rtype,
                "weight": weight,
                "cross_doc_count": row[1] if row else 0,
            })
        return neighbors

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
