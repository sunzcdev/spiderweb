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

    async def discover(self, query: str) -> dict:
        """搜一切。自然语言 query → 意图分类 → 分层搜索 → 合并 → 摘要。"""
        intent = await self._analyze_intent(query)

        if intent == "status":
            return await self._discover_status(query)

        # Intent-based layer search
        layers = await self._search_layers(query, intent)

        # Expand entity detail for lookup
        if intent == "lookup" and layers.get("entities"):
            top = layers["entities"][0]
            detail = entity_get(self.db, top["name"])
            if detail:
                top["description"] = detail.get("description", "")
                top["books"] = detail.get("books", [])
                top["relations"] = detail.get("key_relations", [])[:5]

        # Navigate for compare / explore
        if intent in ("compare", "explore") and layers.get("entities"):
            top = layers["entities"][0]
            depth = 1 if intent == "compare" else 2
            nav = graph_navigate(self.db, top["name"], depth=depth)
            if nav:
                layers["graph"] = nav

        # Merge & standardize
        result = self._merge_results(intent, layers)
        result["query"] = query
        result["intent"] = intent

        # Summary (not for explore — detail mode)
        if intent != "explore":
            result["summary"] = await self._generate_summary(intent, result)

        self._record_query_history(query, intent, result)
        return result

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

    # ── Intent analysis ─────────────────────────────────────────

    async def _analyze_intent(self, query: str) -> str:
        """Classify query intent via LLM, with regex fallback."""
        prompt = load_prompt(self.config, "intent_classify.md")
        if prompt:
            prompt = prompt.replace("{query}", query)
        else:
            prompt = _INTENT_FALLBACK_PROMPT.replace("{query}", query)

        try:
            resp = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0, max_tokens=20,
            )
            if resp:
                data = json.loads(resp)
                if isinstance(data, dict):
                    intent = data.get("intent", "").strip().lower()
                    if intent in ("lookup", "compare", "recall", "explore", "status"):
                        return intent
        except Exception:
            pass

        # Regex fallback
        return self._regex_intent(query)

    def _regex_intent(self, query: str) -> str:
        q = query.lower()
        # recall patterns: 之前记过/我的笔记/我写过/我记得
        if re.search(r'(之前|以前).*(记|写|笔记|想法|心得)', q) or \
           re.search(r'(我的|我.*的).*(笔记|想法|心得|观点|记录)', q) or \
           re.search(r'(翻查|翻看|翻翻|回顾).*(记|笔记|心得)', q):
            return "recall"
        # compare patterns: 区别/对比/比较/比一比/vs/versus
        if re.search(r'(区别|差异|不同|对比|比较|比一比|vs|versus)', q):
            return "compare"
        # explore patterns: 探索/关系/关联/网络/看看周围
        if re.search(r'(探索|关系网|关联|网络|展开|周围|邻居)', q):
            return "explore"
        # status patterns: 概况/统计/进度/有多少/哪些书
        if re.search(r'(概况|统计|进度|有多少|几本|哪些书|状态)', q):
            return "status"
        return "lookup"

    # ── Layer search ────────────────────────────────────────────

    async def _search_layers(self, query: str, intent: str) -> dict:
        """Search relevant layers based on intent. Returns {passages?, entities?, insights?}."""
        layers = {}

        if intent in ("lookup", "compare"):
            # Embed once, share across all layers
            query_vec = await self._embed_query(query)
            # Entities: discover semantically via chunk vec_search
            layers["entities"] = await self._search_entities_semantic(query, query_vec)
            # L1 and L3 have real IO — parallel gather
            tasks = [
                asyncio.create_task(self._search_chunks(query, query_vec)),
                asyncio.create_task(self._search_insights(query, query_vec)),
            ]
            done = await asyncio.gather(*tasks, return_exceptions=True)
            if not isinstance(done[0], BaseException):
                layers["passages"] = done[0]
            if not isinstance(done[1], BaseException):
                layers["insights"] = done[1]

        elif intent == "recall":
            # Insights only
            ins = await self._search_insights(query)
            if not isinstance(ins, BaseException):
                layers["insights"] = ins

        elif intent == "explore":
            # Entities only — embed + semantic search
            query_vec = await self._embed_query(query)
            layers["entities"] = await self._search_entities_semantic(query, query_vec)

        return layers

    async def _search_entities_semantic(self, query: str,
                                         query_vec: list[float] | None = None) -> list[dict]:
        """Discover entities via semantic chunk search.

        Embeds the query, finds semantically relevant chunks, then extracts
        entity mentions from those chunks. Avoids character-level word fragments.
        Falls back to LIKE on the full query, then on whitespace-separated terms.
        """
        # Fallback: try full query, then whitespace terms (handles "王阳明 知行合一")
        def _like_fallback(q):
            ents = search_entities(self.db, q)
            if not ents:
                for term in q.split():
                    ents = search_entities(self.db, term)
                    if ents:
                        break
            return self._standardize_entities(ents)

        if query_vec is None:
            query_vec = await self._embed_query(query)
        if not query_vec:
            return _like_fallback(query)

        tokenizer = create_tokenizer_provider(self.config.tokenizer)
        reranker = create_reranker_provider(self.config.reranker)
        chunks = await hybrid_search(
            self.db, query, query_vec, top_n=10,
            tokenizer=tokenizer, reranker=reranker, body_max_len=200,
        )
        if not chunks:
            return _like_fallback(query)

        chunk_ids = [c["chunk_id"] for c in chunks]
        placeholders = ",".join("?" * len(chunk_ids))
        rows = self.db.execute(f"""
            SELECT DISTINCT e.canonical_name, e.entity_type, e.description
            FROM entities e
            WHERE EXISTS (
                SELECT 1 FROM chunks c
                WHERE c.id IN ({placeholders})
                AND c.body LIKE '%' || e.canonical_name || '%'
            )
        """, chunk_ids).fetchall()
        return [{"name": r[0], "type": r[1], "description": r[2] or ""} for r in rows]

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

    # ── Result formatting ───────────────────────────────────────

    def _merge_results(self, intent: str, layers: dict) -> dict:
        """Standardize and dedup across layers."""
        result = {}

        # Passages: as-is from L1
        if layers.get("passages"):
            result["passages"] = layers["passages"]

        # Entities: dedup by name across entity results + graph
        seen_names = set()
        entities = []
        for e in layers.get("entities", []):
            name = e.get("name") or e.get("entity") or ""
            if name and name not in seen_names:
                seen_names.add(name)
                entities.append({
                    "name": name,
                    "type": e.get("type", "concept"),
                    "description": e.get("description"),
                    "books": e.get("books"),
                    "relations": e.get("relations"),
                })
        # Add graph neighbors as entities (for explore/compare)
        graph = layers.get("graph")
        if graph:
            for entry in graph.get("anchored", []):
                n = entry["neighbor"]
                if n not in seen_names:
                    seen_names.add(n)
                    entities.append({"name": n, "type": "", "description": entry.get("relation", "")})
            for entry in graph.get("exploration", []):
                n = entry["neighbor"]
                if n not in seen_names:
                    seen_names.add(n)
                    entities.append({"name": n, "type": "", "description": f"(线索) {entry.get('relation', '')}"})

        if entities:
            result["entities"] = entities

        # Insights: as-is from L3
        if layers.get("insights"):
            result["insights"] = layers["insights"]

        # Graph (for explore mode detail)
        if graph:
            result["graph"] = {
                "position": graph.get("position"),
                "anchored": graph.get("anchored", []),
                "exploration": graph.get("exploration", []),
            }

        return result

    def _standardize_entities(self, raw: list[dict]) -> list[dict]:
        return [{
            "name": e["name"],
            "type": e["type"],
            "description": e.get("description", ""),
        } for e in raw]

    # ── Summary ─────────────────────────────────────────────────

    async def _generate_summary(self, intent: str, result: dict) -> str | None:
        """Generate a natural-language summary of discover results."""
        prompt = load_prompt(self.config, "summarize.md")
        if not prompt:
            return None

        # Truncate passage bodies in summary context to keep prompt short
        trunc = lambda d: {**d, "body": d.get("body", "")[:200]} if "body" in d else d
        passages = [trunc(p) for p in result.get("passages", [])[:3]]
        context = json.dumps({
            "intent": intent,
            "passages": passages,
            "entities": result.get("entities", [])[:5],
            "insights": result.get("insights", [])[:3],
        }, ensure_ascii=False)

        filled = prompt.replace("{intent}", intent).replace("{results}", context)
        try:
            text = await self._llm.chat(
                [{"role": "user", "content": filled}],
                temperature=0.3, max_tokens=300,
            )
            if text:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data.get("summary", "")
                return str(data)
            return None
        except Exception:
            return None

    # ── Query history ───────────────────────────────────────────

    def _record_query_history(self, query: str, intent: str, result: dict):
        """Record a discover query to query_history for interest graph."""
        summary = {
            "intent": intent,
            "passage_count": len(result.get("passages", [])),
            "entity_count": len(result.get("entities", [])),
            "insight_count": len(result.get("insights", [])),
        }
        self.db.execute(
            "INSERT INTO query_history (query_text, tool_name, docs_json, entities_json, top_results_json) "
            "VALUES (?, 'discover', '[]', '[]', ?)",
            (query, json.dumps(summary, ensure_ascii=False))
        )
        self.db.commit()


# Fallback prompt (used if prompts/intent_classify.md doesn't exist)
_INTENT_FALLBACK_PROMPT = """判断用户的搜索意图。输出一个词。
- lookup: 想了解人物/概念/内容
- compare: 比较两个或多个事物
- recall: 翻查自己之前记过的观点
- explore: 想探索关联和网络
- status: 想看概况或进度

query: {query}
意图："""
