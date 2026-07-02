"""Reading service — domain adapter for the reading domain.

Exposes three operations (discover, record, ingest) that encapsulate
the spiderweb L1/L2/L3 engine. Designed to be imported by server.py
and exposed as 3 MCP tools.
"""
import json
import re
import asyncio
from dataclasses import dataclass, field

from spiderweb.engine.domain import DomainConfig, load_prompt
from spiderweb.engine.providers import (
    create_llm_provider, create_embedding_provider,
    create_tokenizer_provider, create_reranker_provider,
    LLMProvider,
)
from spiderweb.engine.l1.ingest import ingest_file, index_vectors, hybrid_search, ensure_vec_table
from spiderweb.engine.l2.build import build_graph
from spiderweb.engine.l2.search import search_entities, entity_get, graph_navigate, graph_stats
from spiderweb.engine.l3.search import search_insights
from spiderweb.engine.l3.insight import write_insight


@dataclass
class ReadingService:
    """读书郎的阅读服务 — 封装三个领域操作。

    Usage:
        service = ReadingService(db, config)
        await service.discover("王阳明是谁")
        await service.record("知行合一", source="传习录")
        await service.ingest("/path/to/book.epub")
    """
    db: object
    config: DomainConfig
    _llm: LLMProvider = field(init=False)

    def __post_init__(self):
        self._llm = create_llm_provider(self.config.llm)

    # ── Public API ──────────────────────────────────────────────

    async def discover(self, query: str, mode: str = "summary") -> dict:
        """搜一切。summary→图谱脉络，detail→原文详情。"""
        if re.search(r'(概况|统计|进度|有多少|几本|哪些书|状态)', query):
            return await self._discover_status(query)

        result = await (self._discover_detail(query) if mode == "detail"
                       else self._discover_summary(query))
        self._record_query_history(query, result)
        return result

    async def _discover_summary(self, query: str) -> dict:
        """Summary: seed entity → graph navigation → LLM narrative."""
        seed = self._find_seed_entity(query)
        result = {"mode": "summary", "query": query, "seed": seed}
        if seed:
            nav = graph_navigate(self.db, seed, depth=2)
            if nav:
                result["graph"] = {
                    "position": nav.get("position"),
                    "anchored": nav.get("anchored", []),
                    "exploration": nav.get("exploration", []),
                }
                result["narrative"] = await self._generate_graph_narrative(query, result["graph"])
        return result

    async def _discover_detail(self, query: str) -> dict:
        """Detail: L1 passages + L3 insights."""
        query_vec = await self._embed_query(query)
        tasks = [
            asyncio.create_task(self._search_chunks(query, query_vec)),
            asyncio.create_task(self._search_insights(query, query_vec)),
        ]
        done = await asyncio.gather(*tasks, return_exceptions=True)
        result = {"mode": "detail", "query": query}
        if not isinstance(done[0], BaseException):
            result["passages"] = done[0]
        if not isinstance(done[1], BaseException):
            result["insights"] = done[1]
        return result

    def _find_seed_entity(self, query: str) -> str | None:
        """Find best matching entity name from query for graph seeding."""
        ents = search_entities(self.db, query)
        if ents:
            return ents[0]["name"]
        for term in query.split():
            ents = search_entities(self.db, term)
            if ents:
                return ents[0]["name"]
        # CJK compound without spaces: try progressively shorter prefixes
        # "阳明知行合一" → "阳明知行合" → "阳明知行" → "阳明" (match)
        for i in range(len(query) - 1, 1, -1):
            sub = query[:i]
            ents = search_entities(self.db, sub)
            if ents:
                return ents[0]["name"]
        return None

    async def record(self, content: str, source: str | None = None) -> dict:
        """记一条心得。引擎自动抽实体、关联到 L2。"""
        if not content or not content.strip():
            return {"ok": False, "error": "content is empty"}
        title = content[:50].strip().split("\n")[0]
        source_docs = [source] if source else []
        return await write_insight(self.db, self.config, title, content, source_docs)

    async def ingest(self, source: str) -> dict:
        """收录新书。解析、分块、索引、建图，同步等到底。"""
        from spiderweb.engine.db import get_db_path

        file_path = source
        config = self.config
        db = self.db

        # Dedup: if same path exists, delete and reimport
        existing = db.execute(
            "SELECT id, title FROM docs WHERE path = ?", (file_path,)
        ).fetchone()
        if existing:
            doc_id = existing[0]
            chunk_ids = [
                r[0] for r in db.execute(
                    "SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)
                ).fetchall()
            ]
            for cid in chunk_ids:
                try:
                    db.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
                except Exception:
                    pass
            db.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id = ?)",
                (doc_id,)
            )
            db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            db.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
            db.commit()

        # Parse & chunk
        title, author, chunks = ingest_file(
            file_path,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            hooks_module=config.hooks_module,
        )

        # Insert doc
        cursor = db.execute(
            "INSERT INTO docs (path, title, author) VALUES (?, ?, ?)",
            (file_path, title, author)
        )
        doc_id = cursor.lastrowid

        # Insert chunks + FTS5
        chunk_ids = []
        tokenizer = create_tokenizer_provider(config.tokenizer)
        for ch in chunks:
            cid = db.execute(
                "INSERT INTO chunks (doc_id, section_path, heading_level, body, line_start) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, ch.section_path, ch.heading_level, ch.body, ch.line_start)
            ).lastrowid
            fts_body = tokenizer.tokenize(ch.body) if tokenizer else ch.body
            db.execute(
                "INSERT INTO chunks_fts(rowid, body) VALUES (?, ?)",
                (cid, fts_body)
            )
            chunk_ids.append(cid)

        db.commit()

        # Vector index (sync)
        vec_count = 0
        emb_provider = create_embedding_provider(config.embedding)
        if emb_provider:
            try:
                vec_count = await index_vectors(db, emb_provider, chunk_ids)
            except Exception as e:
                vec_count = 0

        # Build graph (sync)
        graph_result = {}
        try:
            graph_result = await build_graph(db, config, doc_ids=[doc_id])
        except Exception as e:
            graph_result = {"error": str(e)}

        return {
            "ok": True,
            "doc_id": doc_id,
            "title": title,
            "author": author,
            "chunks": len(chunks),
            "vectors": vec_count,
            "entities_found": graph_result.get("entities_found", 0),
            "relations_added": graph_result.get("relations_added", 0),
        }

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

    async def _search_chunks(self, query: str, query_vec: list[float] | None = None,
                             top_n: int = 5) -> list[dict]:
        """Search L1 passages with full body."""
        if query_vec is None:
            query_vec = await self._embed_query(query)
        tokenizer = create_tokenizer_provider(self.config.tokenizer)
        reranker = create_reranker_provider(self.config.reranker)
        results = await hybrid_search(
            self.db, query, query_vec, top_n, tokenizer, reranker,
            body_max_len=None,
        )
        return [{"book": r["doc_title"], "section": r["section"], "body": r["body"]}
                for r in results]

    async def _search_insights(self, query: str, query_vec: list[float] | None = None,
                               top_n: int = 5) -> list[dict]:
        """Search L3 insights, return standardized format."""
        if query_vec is not None:
            results = await search_insights(self.db, query, self.config, top_n, query_vec)
        else:
            results = await search_insights(self.db, query, self.config, top_n)
        return [{"title": r["title"], "content": r["content"]} for r in results]

    # ── Status ──────────────────────────────────────────────────

    async def _discover_status(self, query: str) -> dict:
        """Status intent: graph stats + doc list."""
        stats = graph_stats(self.db)
        docs = self.db.execute(
            "SELECT title, author FROM docs ORDER BY id DESC"
        ).fetchall()
        return {
            "query": query,
            "intent": "status",
            "stats": stats,
            "books": [{"title": d[0], "author": d[1]} for d in docs],
            "summary": f"共 {stats.get('docs', 0)} 本书、"
                       f"{stats.get('entities', 0)} 个实体、"
                       f"{stats.get('relations', 0)} 条关系、"
                       f"{stats.get('insights', 0)} 条心得",
        }


    # ── Graph narrative ─────────────────────────────────────────

    async def _generate_graph_narrative(self, query: str, graph: dict) -> str | None:
        """LLM generates a narrative explaining entity connections from the graph."""
        prompt = load_prompt(self.config, "graph_narrative.md")
        if not prompt:
            prompt = _GRAPH_NARRATIVE_FALLBACK
        context = json.dumps(graph, ensure_ascii=False)
        filled = prompt.replace("{query}", query).replace("{graph}", context)
        try:
            text = await self._llm.chat(
                [{"role": "user", "content": filled}],
                temperature=0.3, max_tokens=300,
            )
            if text:
                # Strip markdown fences before JSON parse (LLMs often wrap in ```json)
                text = text.strip()
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                if m:
                    text = m.group(1).strip()
                data = json.loads(text)
                if isinstance(data, dict):
                    return data.get("narrative", "")
                return str(data)
            return None
        except Exception:
            return None

    # ── Query history ───────────────────────────────────────────

    def _record_query_history(self, query: str, result: dict):
        """Record a discover query to query_history for interest graph."""
        mode = result.get("mode", "summary")
        summary = {
            "mode": mode,
            "passage_count": len(result.get("passages", [])),
            "insight_count": len(result.get("insights", [])),
        }
        self.db.execute(
            "INSERT INTO query_history (query_text, tool_name, docs_json, entities_json, top_results_json) "
            "VALUES (?, 'discover', '[]', '[]', ?)",
            (query, json.dumps(summary, ensure_ascii=False))
        )
        self.db.commit()


_GRAPH_NARRATIVE_FALLBACK = """你是一个阅读助手，帮用户理解知识图谱中的关联。

用户查询：{query}

知识图谱：
{graph}

解释图谱中的实体之间的逻辑关系。像说故事一样连贯，控制在200字以内。输出JSON格式：{{"narrative": "你的叙述"}}"""
