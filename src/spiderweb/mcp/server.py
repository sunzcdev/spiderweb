"""MCP stdio server for Spiderweb."""
import os
import sys
import json
import time
import argparse
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ProgressNotification

from ..engine.db import get_db, get_db_path
from ..debug import init as debug_init, tool_call as debug_call, tool_result as debug_result, tool_error as debug_error
from ..engine.domain import load_domain, DomainConfig
from ..engine.l1.ingest import ingest_file, index_vectors, hybrid_search
from ..engine.l2.build import build_graph
from ..engine.l3.insight import reverse_extract, link_insight_to_entities
from ..engine.providers import create_llm_provider, create_embedding_provider, create_tokenizer_provider, create_reranker_provider

server = Server("spiderweb")
_config: DomainConfig = None


def get_config() -> DomainConfig:
    global _config
    if _config is None:
        domain_path = os.environ.get("SPIDERWEB_DOMAIN", "")
        if not domain_path:
            raise RuntimeError("SPIDERWEB_DOMAIN not set and --domain not provided")
        _config = load_domain(domain_path)
    return _config


def _json_result(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@server.list_tools()
async def list_tools():
    return [
        # ── L1: document layer ──
        Tool(name="doc_ingest", description="Ingest a document (.md/.txt/.epub). Synchronous: returns after vector indexing + graph extraction complete, with progress notifications.", inputSchema={"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the document file"}, "title": {"type": "string", "description": "Optional title override"}, "author": {"type": "string", "description": "Optional author"}}}),
        Tool(name="search_chunks", description="Search document chunks using hybrid FTS5 + LIKE + vector search", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "top_n": {"type": "integer", "default": 5, "description": "Number of results"}}}),
        # ── L2: knowledge graph layer ──
        Tool(name="search_entities", description="Search entities by name (fuzzy match + aliases)", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Entity name to search"}}}),
        Tool(name="entity_get", description="Get entity details: type, aliases, books, key relations, related insights, footprint", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Entity canonical name"}}}),
        Tool(name="graph_navigate", description="Navigate knowledge graph from an entity: anchored edges + exploration edges, with path trace and depth support", inputSchema={"type": "object", "properties": {"seed": {"type": "string", "description": "Starting entity name"}, "depth": {"type": "integer", "default": 1, "description": "Hops from seed (1=neighbors only, 2=two-hop path)"}}}),
        Tool(name="connect", description="Find shortest path between two entities in the knowledge graph (BFS)", inputSchema={"type": "object", "properties": {"from": {"type": "string", "description": "Starting entity"}, "to": {"type": "string", "description": "Target entity"}}}),
        # ── L3: insight layer ──
        Tool(name="insight_record", description="Record an insight; auto-extracts entities and links to L2 in background", inputSchema={"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}}}),
        Tool(name="insight_list", description="List recent insights", inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}),
        Tool(name="search_insights", description="Search insights by title and content", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "top_n": {"type": "integer", "default": 5, "description": "Number of results"}}}),
        # ── Meta ──
        Tool(name="graph_stats", description="Get graph statistics: doc count, entity count, relation count, insight count", inputSchema={"type": "object", "properties": {}}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict, context=None):
    config = get_config()
    # Lazy debug init (also called in main() for MCP startup)
    if os.environ.get("SPIDERWEB_DEBUG") == "1":
        debug_init(config.data_dir)

    debug_call(name, arguments)
    t0 = time.time()
    db_path = get_db_path(config.data_dir)
    db = get_db(db_path)

    # Progress reporter for long-running tools
    async def progress(ratio: float, msg: str):
        if context:
            try:
                await context.session.send_notification(
                    ProgressNotification(progress=ratio, total=1.0, message=msg)
                )
            except Exception:
                pass

    try:
        if name == "graph_stats":
            result = await _graph_stats(db)
        elif name == "search_chunks":
            result = await _search_chunks(db, arguments)
        elif name == "search_entities":
            result = await _search_entities(db, arguments)
        elif name == "search_insights":
            result = await _search_insights(db, arguments)
        elif name == "doc_ingest":
            result = await _doc_ingest(db, config, arguments, progress)
        elif name == "doc_get":
            result = await _doc_get(db, arguments)
        elif name == "doc_delete":
            result = await _doc_delete(db, arguments)
        elif name == "graph_build":
            result = await _graph_build(db, config, arguments)
        elif name == "entity_register":
            result = await _entity_register(db, arguments)
        elif name == "relation_set":
            result = await _relation_set(db, arguments)
        elif name == "relation_list":
            result = await _relation_list(db, arguments)
        elif name == "graph_navigate":
            result = await _graph_navigate(db, config, arguments)
        elif name == "insight_record":
            result = await _insight_record(db, config, arguments, progress)
        elif name == "entity_get":
            result = await _entity_get(db, arguments)
        elif name == "connect":
            result = await _connect(db, arguments)
        elif name == "insight_list":
            result = await _insight_list(db, arguments)
        else:
            result = [TextContent(type="text", text=f"Unknown tool: {name}")]

        elapsed = (time.time() - t0) * 1000
        debug_result(name, result, elapsed)
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        debug_error(name, str(e), elapsed)
        return [TextContent(type="text", text=f"Error: {e}")]


# === Tool implementations ===

async def _doc_ingest(db, config: DomainConfig, args, progress=None) -> list[TextContent]:
    file_path = os.path.expanduser(args["file_path"])
    title_override = args.get("title", "")
    author_override = args.get("author", "")

    async def _p(ratio, msg):
        if progress:
            await progress(ratio, msg)

    # Auto-dedup: if same file path was ingested before, delete old L1 first
    replaced = False
    existing = db.execute("SELECT id, title FROM docs WHERE path = ?", (file_path,)).fetchone()
    if existing:
        print(f"[spiderweb] doc_ingest: replacing existing doc #{existing[0]} '{existing[1]}' (same path)", file=sys.stderr)
        await _doc_delete(db, {"doc_id": existing[0]})
        replaced = True

    title, author, chunks = ingest_file(
        file_path,
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        hooks_module=config.hooks_module,
    )

    if title_override:
        title = title_override
    if author_override:
        author = author_override

    # Insert doc
    await _p(0.05, f"解析完成：{title}")
    cursor = db.execute(
        "INSERT INTO docs (path, title, author) VALUES (?, ?, ?)",
        (file_path, title, author)
    )
    doc_id = cursor.lastrowid

    # Insert chunks
    chunk_count = 0
    chunk_ids = []
    tokenizer = create_tokenizer_provider(config.tokenizer)
    for ch in chunks:
        cid = db.execute(
            "INSERT INTO chunks (doc_id, section_path, heading_level, body, line_start) VALUES (?, ?, ?, ?, ?)",
            (doc_id, ch.section_path, ch.heading_level, ch.body, ch.line_start)
        ).lastrowid
        # FTS5: use tokenized text if tokenizer available (Chinese word segmentation)
        fts_body = tokenizer.tokenize(ch.body) if tokenizer else ch.body
        db.execute(
            "INSERT INTO chunks_fts(rowid, body) VALUES (?, ?)",
            (cid, fts_body)
        )
        chunk_ids.append(cid)
        chunk_count += 1

    await _p(0.1, f"段落索引完成：{chunk_count} 段")

    # Vector indexing (if embedding provider configured)
    vec_count = 0
    emb_provider = create_embedding_provider(config.embedding)
    if emb_provider:
        await _p(0.15, f"开始向量化：{chunk_count} 段...")
        try:
            vec_count = await index_vectors(db, emb_provider, chunk_ids)
            print(f"[spiderweb] doc_ingest #{doc_id}: {vec_count} vectors indexed", file=sys.stderr)
        except Exception as e:
            print(f"[spiderweb] doc_ingest #{doc_id}: vector indexing failed: {e}", file=sys.stderr)

    db.commit()
    await _p(0.5, "向量化完成，开始建网...")

    # Auto graph_build: extract entities + relations from the just-ingested doc
    graph_result = None
    try:
        graph_result = await build_graph(db, config, doc_ids=[doc_id])
        await _p(0.95, f"建网完成：{graph_result.get('entities_found', 0)} 实体 / {graph_result.get('relations_added', 0)} 关系")
        print(f"[spiderweb] doc_ingest #{doc_id}: graph_build done — {graph_result.get('entities_found', 0)} entities", file=sys.stderr)
    except Exception as e:
        await _p(0.95, "建网失败（书已入库，可稍后重建）")
        print(f"[spiderweb] doc_ingest #{doc_id}: graph_build failed: {e}", file=sys.stderr)

    return [TextContent(type="text", text=_json_result({
        "ok": True,
        "doc_id": doc_id,
        "title": title,
        "author": author,
        "chunks": chunk_count,
        "vectors": vec_count,
        "graph": graph_result,
        "replaced": replaced,
    }))]


async def _graph_build(db, config: DomainConfig, args) -> list[TextContent]:
    doc_ids = args.get("doc_ids", [])
    if not doc_ids:
        return [TextContent(type="text", text=_json_result({
            "error": "doc_ids required — pass explicit doc IDs, or use 'all' to process everything"
        }))]
    if doc_ids == ["all"]:
        doc_ids = None  # process all
    result = await build_graph(db, config, doc_ids=doc_ids if doc_ids else None)
    return [TextContent(type="text", text=_json_result(result))]


async def _entity_register(db, args) -> list[TextContent]:
    name = args["name"].strip()
    etype = args.get("entity_type", "concept")
    aliases = json.dumps(args.get("aliases", []), ensure_ascii=False)
    description = args.get("description", "")

    existing = db.execute("SELECT id FROM entities WHERE canonical_name = ?", (name,)).fetchone()
    if existing:
        db.execute(
            "UPDATE entities SET entity_type = ?, aliases_json = ?, description = ? WHERE canonical_name = ?",
            (etype, aliases, description, name)
        )
    else:
        db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json, description, source) VALUES (?, ?, ?, ?, 'manual')",
            (name, etype, aliases, description)
        )
    db.commit()
    return [TextContent(type="text", text=_json_result({"ok": True, "name": name, "type": etype}))]


async def _graph_stats(db) -> list[TextContent]:
    stats = {}
    for table, label in [("docs", "docs"), ("entities", "entities"),
                          ("relations", "relations"), ("insights", "insights")]:
        row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        stats[label] = row[0]
    # Relation types breakdown
    types = db.execute("SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type").fetchall()
    stats["relation_types"] = {t: c for t, c in types}
    return [TextContent(type="text", text=_json_result(stats))]


async def _search_chunks(db, args) -> list[TextContent]:
    query = args["query"]
    top_n = args.get("top_n", 5)
    config = get_config()

    # Get query embedding if provider configured
    query_vec = None
    emb_provider = create_embedding_provider(config.embedding)
    if emb_provider:
        try:
            vecs = await emb_provider.embed([query])
            query_vec = vecs[0] if vecs else None
        except Exception:
            pass

    tokenizer = create_tokenizer_provider(config.tokenizer)
    reranker = create_reranker_provider(config.reranker)
    results = await hybrid_search(db, query, query_vec, top_n, tokenizer, reranker)
    _record_query_history(db, query, "search_chunks", [{"chunk_id": r["chunk_id"], "doc_title": r["doc_title"]} for r in results[:5]])
    return [TextContent(type="text", text=_json_result({"results": results, "count": len(results)}))]


async def _search_entities(db, args) -> list[TextContent]:
    query = args["query"]
    entity_type = args.get("entity_type", "")
    sql = "SELECT canonical_name, entity_type, aliases_json, description, cross_doc_count FROM entities WHERE (canonical_name LIKE ? OR aliases_json LIKE ?)"
    params = [f"%{query}%", f"%{query}%"]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    rows = db.execute(sql, params).fetchall()
    results = [{"name": r[0], "type": r[1], "aliases": json.loads(r[2]),
                "description": r[3], "cross_doc_count": r[4]} for r in rows]
    _record_query_history(db, query, "search_entities", [{"name": r["name"], "type": r["type"]} for r in results[:10]])
    return [TextContent(type="text", text=_json_result({"results": results, "count": len(results)}))]


async def _search_insights(db, args) -> list[TextContent]:
    query = args["query"]
    top_n = args.get("top_n", 5)
    config = get_config()
    results = []
    seen = set()

    # FTS5 (jieba-tokenized, word-level precision)
    tokenizer = create_tokenizer_provider(config.tokenizer)
    if tokenizer:
        tokenized = tokenizer.tokenize(query)
        tokens = tokenized.split()
        fts_query = " ".join(f"{t}*" for t in tokens) if tokens else query
    else:
        fts_query = query

    try:
        fts_rows = db.execute(
            "SELECT i.id, i.slug, i.title, i.content, i.created_at FROM insights i "
            "JOIN insights_fts f ON i.rowid = f.rowid "
            "WHERE insights_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, top_n * 2)
        ).fetchall()
        for r in fts_rows:
            if r[0] not in seen:
                seen.add(r[0])
                results.append({"id": r[0], "slug": r[1], "title": r[2],
                               "content": r[3][:300], "created_at": r[4], "source": "fts"})
    except Exception:
        pass

    # LIKE fallback (substring recall)
    like_rows = db.execute(
        "SELECT id, slug, title, content, created_at FROM insights "
        "WHERE title LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", f"%{query}%", top_n * 2)
    ).fetchall()
    for r in like_rows:
        if r[0] not in seen:
            seen.add(r[0])
            results.append({"id": r[0], "slug": r[1], "title": r[2],
                           "content": r[3][:300], "created_at": r[4], "source": "like"})

    # Vector search (semantic)
    emb_provider = create_embedding_provider(config.embedding)
    if emb_provider:
        has_vec = db.execute("SELECT value FROM _meta WHERE key = 'has_vectors_insights_vec'").fetchone()
        if has_vec and has_vec[0] == '1':
            try:
                vecs = await emb_provider.embed([query])
                query_vec = vecs[0]
                vec_rows = db.execute(
                    "SELECT i.id, i.slug, i.title, i.content, i.created_at, v.distance "
                    "FROM insights_vec v "
                    "JOIN insights i ON v.rowid = i.id "
                    "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                    (json.dumps(query_vec), top_n * 2)
                ).fetchall()
                for r in vec_rows:
                    if r[0] not in seen:
                        seen.add(r[0])
                        results.append({"id": r[0], "slug": r[1], "title": r[2],
                                       "content": r[3][:300], "created_at": r[4], "source": "vector"})
            except Exception:
                pass

    results = results[:top_n]
    _record_query_history(db, query, "search_insights", [{"slug": r["slug"], "title": r["title"]} for r in results[:5]])
    return [TextContent(type="text", text=_json_result({"results": results, "count": len(results)}))]


async def _doc_get(db, args) -> list[TextContent]:
    chunk_id = args["chunk_id"]
    row = db.execute(
        "SELECT c.id, c.body, c.section_path, c.heading_level, d.title "
        "FROM chunks c JOIN docs d ON c.doc_id = d.id WHERE c.id = ?",
        (chunk_id,)
    ).fetchone()
    if not row:
        return [TextContent(type="text", text="Chunk not found")]
    return [TextContent(type="text", text=_json_result({
        "chunk_id": row[0], "body": row[1], "section": row[2],
        "heading_level": row[3], "doc_title": row[4]
    }))]


async def _doc_delete(db, args) -> list[TextContent]:
    doc_id = args["doc_id"]

    # Get doc info before deleting
    doc = db.execute("SELECT title FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        return [TextContent(type="text", text=_json_result({"error": "not_found", "doc_id": doc_id}))]

    title = doc[0]
    chunk_count = db.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0]
    print(f"[spiderweb] doc_delete: #{doc_id} '{title}' — {chunk_count} chunks", file=sys.stderr)

    # Get chunk IDs for this doc
    chunk_ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()]

    # Delete vectors (order: child tables first)
    vec_count = 0
    for cid in chunk_ids:
        try:
            db.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
            vec_count += db.total_changes
        except Exception:
            pass

    # Delete FTS5 index
    db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))

    # Delete chunks
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    # Delete doc
    db.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    db.commit()

    return [TextContent(type="text", text=_json_result({
        "ok": True,
        "doc_id": doc_id,
        "title": title,
        "chunks_deleted": chunk_count,
        "vectors_deleted": vec_count,
    }))]


async def _relation_set(db, args) -> list[TextContent]:
    source_docs = json.dumps(args.get("source_docs", []), ensure_ascii=False)
    weight = args.get("weight", 1.0)
    a, b, rtype = args["entity_a"], args["entity_b"], args["relation_type"]

    # ON CONFLICT: merge source_docs and update weight/last_seen, preserve first_seen
    existing = db.execute(
        "SELECT id, source_docs_json FROM relations WHERE entity_a = ? AND entity_b = ? AND relation_type = ?",
        (a, b, rtype)
    ).fetchone()

    if existing:
        # Merge source docs
        try:
            old_docs = json.loads(existing[1] or "[]")
            new_docs = json.loads(source_docs)
            merged = json.dumps(list(set(old_docs + new_docs)), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            merged = source_docs if source_docs != "[]" else existing[1]

        db.execute(
            "UPDATE relations SET weight = ?, source_docs_json = ?, last_seen = datetime('now') WHERE id = ?",
            (weight, merged, existing[0])
        )
    else:
        db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type, weight, source_docs_json, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            (a, b, rtype, weight, source_docs)
        )

    db.execute("INSERT OR IGNORE INTO entity_traces (entity_name, source, last_seen) VALUES (?, 'relation_set', datetime('now'))", (a,))
    db.execute("INSERT OR IGNORE INTO entity_traces (entity_name, source, last_seen) VALUES (?, 'relation_set', datetime('now'))", (b,))
    db.commit()
    return [TextContent(type="text", text=_json_result({"ok": True}))]


async def _relation_list(db, args) -> list[TextContent]:
    name = args["entity_name"]
    rows = db.execute(
        "SELECT entity_a, entity_b, relation_type, weight FROM relations "
        "WHERE entity_a = ? OR entity_b = ? ORDER BY weight DESC",
        (name, name)
    ).fetchall()
    return [TextContent(type="text", text=_json_result({
        "entity": name,
        "relations": [{"a": r[0], "b": r[1], "type": r[2], "weight": r[3]} for r in rows]
    }))]


async def _graph_navigate(db, config: DomainConfig, args) -> list[TextContent]:
    seed = args["seed"]
    depth = args.get("depth", 1)
    mode = args.get("mode", "explore")

    # Resolve seed
    row = db.execute("SELECT canonical_name, entity_type FROM entities WHERE canonical_name = ?", (seed,)).fetchone()
    if not row:
        row = db.execute("SELECT canonical_name, entity_type FROM entities WHERE canonical_name LIKE ? LIMIT 1", (f"%{seed}%",)).fetchone()
    if not row:
        return [TextContent(type="text", text=_json_result({"error": "not_found", "seed": seed}))]

    name, etype = row
    path_trace = [name]

    def _expand(entity_name):
        edges = db.execute(
            "SELECT entity_a, entity_b, relation_type, weight FROM relations "
            "WHERE entity_a = ? OR entity_b = ? ORDER BY weight DESC",
            (entity_name, entity_name)
        ).fetchall()
        anc, exp = [], []
        for a, b, rtype, weight in edges:
            neighbor = b if a == entity_name else a
            entry = {"neighbor": neighbor, "relation": rtype, "weight": weight}
            has_entry = db.execute("SELECT 1 FROM entities WHERE canonical_name = ?", (neighbor,)).fetchone()
            if has_entry and weight >= 1.0:
                anc.append(entry)
            else:
                exp.append(entry)
        return anc, exp

    anchored, exploration = _expand(name)

    # Depth=2: treat top anchored neighbor as next hop
    if depth >= 2 and anchored:
        next_hop = anchored[0]["neighbor"]
        path_trace.append(next_hop)
        anc2, exp2 = _expand(next_hop)
        anchored = [{"neighbor": n["neighbor"], "relation": f"{name}→{next_hop}→", "weight": n["weight"]} for n in anc2[:10]]
        exploration = exploration[:10] + exp2[:10]

    # Record footprints
    _record_trace(db, name)
    for entry in anchored[:15] + exploration[:20]:
        _record_trace(db, entry["neighbor"])

    return [TextContent(type="text", text=_json_result({
        "position": {"entity": name, "type": etype},
        "anchored": anchored[:15],
        "exploration": exploration[:20],
        "path_trace": path_trace,
        "is_end": len(anchored) == 0 and len(exploration) == 0
    }))]


def _record_trace(db, entity_name):
    """Record one footprint for an entity."""
    db.execute(
        "INSERT INTO entity_traces (entity_name, source, count, last_seen) "
        "VALUES (?, 'navigate', 1, datetime('now')) "
        "ON CONFLICT(entity_name, source) DO UPDATE SET count = count + 1, last_seen = datetime('now')",
        (entity_name,)
    )


def _record_query_history(db, query: str, tool_name: str, results: list[dict]):
    """Record search footprint for interest graph."""
    db.execute(
        "INSERT INTO query_history (query_text, tool_name, top_results_json) VALUES (?, ?, ?)",
        (query, tool_name, json.dumps(results, ensure_ascii=False))
    )
    db.commit()


async def _entity_get(db, args) -> list[TextContent]:
    """Get entity detail: type, aliases, description, key relations, related insights, footprint."""
    name = args["name"].strip()
    row = db.execute(
        "SELECT canonical_name, entity_type, aliases_json, description, source, cross_doc_count "
        "FROM entities WHERE canonical_name = ?", (name,)
    ).fetchone()
    if not row:
        return [TextContent(type="text", text=_json_result({"error": "not_found", "name": name}))]

    # Top relations (by weight)
    relations = db.execute(
        "SELECT entity_a, entity_b, relation_type, weight FROM relations "
        "WHERE entity_a = ? OR entity_b = ? ORDER BY weight DESC LIMIT 20",
        (name, name)
    ).fetchall()
    key_relations = []
    for a, b, rtype, w in relations:
        neighbor = b if a == name else a
        key_relations.append({"entity": neighbor, "relation": rtype, "weight": w})

    # Related insights (via entities_json)
    insights = db.execute(
        "SELECT slug, title FROM insights WHERE entities_json LIKE ? LIMIT 10",
        (f"%{name}%",)
    ).fetchall()
    related_insights = [{"slug": r[0], "title": r[1]} for r in insights]

    # Footprint
    trace = db.execute(
        "SELECT source, count, last_seen FROM entity_traces WHERE entity_name = ?", (name,)
    ).fetchall()
    footprint = [{"source": t[0], "count": t[1], "last_seen": t[2]} for t in trace]

    # Books this entity appears in
    books = db.execute(
        "SELECT DISTINCT d.title FROM chunks c JOIN docs d ON c.doc_id = d.id "
        "WHERE c.body LIKE ? LIMIT 10", (f"%{name}%",)
    ).fetchall()
    book_list = [b[0] for b in books]

    return [TextContent(type="text", text=_json_result({
        "name": row[0], "type": row[1],
        "aliases": json.loads(row[2] or "[]"),
        "description": row[3], "source": row[4],
        "cross_doc_count": row[5],
        "books": book_list,
        "key_relations": key_relations,
        "related_insights": related_insights,
        "footprint": footprint,
    }))]


async def _connect(db, args) -> list[TextContent]:
    """BFS shortest path between two entities in the knowledge graph."""
    start = args["from"].strip()
    end = args["to"].strip()

    # Verify both exist
    a = db.execute("SELECT 1 FROM entities WHERE canonical_name = ?", (start,)).fetchone()
    b = db.execute("SELECT 1 FROM entities WHERE canonical_name = ?", (end,)).fetchone()
    if not a:
        return [TextContent(type="text", text=_json_result({"error": "not_found", "entity": start}))]
    if not b:
        return [TextContent(type="text", text=_json_result({"error": "not_found", "entity": end}))]
    if start == end:
        return [TextContent(type="text", text=_json_result({"path": [start], "length": 0}))]

    # Build adjacency list from relations
    adj = {}
    for row in db.execute("SELECT entity_a, entity_b, relation_type FROM relations").fetchall():
        adj.setdefault(row[0], []).append((row[1], row[2]))
        adj.setdefault(row[1], []).append((row[0], row[2]))

    if start not in adj:
        return [TextContent(type="text", text=_json_result({"path": [], "error": "no connections from start"}))]

    # BFS
    from collections import deque
    queue = deque([(start, [start])])
    visited = {start}

    while queue:
        node, path = queue.popleft()
        for neighbor, rtype in adj.get(node, []):
            if neighbor == end:
                return [TextContent(type="text", text=_json_result({
                    "path": path + [neighbor], "length": len(path)
                }))]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return [TextContent(type="text", text=_json_result({"path": [], "error": "no path found"}))]


async def _insight_record(db, config: DomainConfig, args, progress=None) -> list[TextContent]:
    import re
    title = args["title"]
    content = args["content"]
    source_docs = args.get("source_docs", [])
    slug = re.sub(r'[^a-z0-9一-鿿]+', '-', title.lower().strip())[:80]

    cursor = db.execute(
        "INSERT OR REPLACE INTO insights (slug, title, content, source_docs_json, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (slug, title, content, json.dumps(source_docs, ensure_ascii=False))
    )
    insight_id = cursor.lastrowid

    # FTS5: write jieba-tokenized text for Chinese word segmentation
    tokenizer = create_tokenizer_provider(config.tokenizer)
    fts_title = tokenizer.tokenize(title) if tokenizer else title
    fts_content = tokenizer.tokenize(content) if tokenizer else content
    db.execute(
        "INSERT OR REPLACE INTO insights_fts(rowid, title, content) VALUES (?, ?, ?)",
        (insight_id, fts_title, fts_content)
    )

    # Vector: embed insight content for semantic search
    emb_provider = create_embedding_provider(config.embedding)
    vec_count = 0
    if emb_provider:
        from ..engine.l1.ingest import index_vectors, ensure_vec_table
        ensure_vec_table(db, emb_provider.dimensions, "insights_vec")
        try:
            vecs = await emb_provider.embed([f"{title}\n{content}"])
            db.execute(
                "INSERT OR REPLACE INTO insights_vec(rowid, embedding) VALUES (?, ?)",
                (insight_id, json.dumps(vecs[0]))
            )
            vec_count = 1
        except Exception:
            pass  # non-fatal: insight stored, just no vector

    # Reverse extract entities and link to L2
    link_result = {}
    if progress:
        await progress(0.7, "提取实体中...")
    try:
        llm = create_llm_provider(config.llm)
        entities, relations = await reverse_extract(llm, config, content)
        link_result = link_insight_to_entities(db, config, insight_id, entities, relations)
        if progress:
            await progress(0.95, f"已关联 {link_result.get('entities_extracted', 0)} 个实体")
    except Exception as e:
        link_result = {"error": str(e)}

    # Linked books (from source_docs + entity traces)
    linked_books = list(set(source_docs or []))
    if link_result and "entity_names" in link_result:
        for name in link_result["entity_names"]:
            doc_rows = db.execute(
                "SELECT DISTINCT d.title FROM chunks c JOIN docs d ON c.doc_id = d.id "
                "WHERE c.body LIKE ? LIMIT 3", (f"%{name}%",)
            ).fetchall()
            for (dt,) in doc_rows:
                if dt not in linked_books:
                    linked_books.append(dt)

    db.commit()
    return [TextContent(type="text", text=_json_result({
        "ok": True,
        "slug": slug,
        "insight_id": insight_id,
        "linked": link_result,
        "linked_books": linked_books[:10],
        "vectors": vec_count,
    }))]


async def _insight_list(db, args) -> list[TextContent]:
    limit = args.get("limit", 20)
    rows = db.execute(
        "SELECT id, slug, title, created_at FROM insights ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    results = [{"id": r[0], "slug": r[1], "title": r[2], "created_at": r[3]} for r in rows]
    return [TextContent(type="text", text=_json_result({"insights": results}))]


async def main(domain_path: str | None = None):
    if domain_path:
        os.environ["SPIDERWEB_DOMAIN"] = domain_path

    # Allow --domain flag for direct invocation
    parser = argparse.ArgumentParser(description="Spiderweb MCP server")
    parser.add_argument("--domain", help="Path to domain pack directory")
    args, _ = parser.parse_known_args()
    if args.domain:
        os.environ["SPIDERWEB_DOMAIN"] = args.domain

    if not os.environ.get("SPIDERWEB_DOMAIN"):
        print("Error: Set SPIDERWEB_DOMAIN env var or use --domain", file=sys.stderr)
        sys.exit(1)

    global _config
    _config = load_domain(os.environ["SPIDERWEB_DOMAIN"])

    # Init debug logging
    if os.environ.get("SPIDERWEB_DEBUG") == "1":
        debug_init(_config.data_dir)
        sys.stderr.write(f"[spiderweb] debug logging to {_config.data_dir}/debug.log\n")

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
