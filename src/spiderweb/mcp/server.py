"""MCP stdio server for Spiderweb."""
import os
import sys
import json
import argparse
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from ..engine.db import get_db, get_db_path
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
        Tool(name="graph_stats", description="Get graph statistics: doc count, entity count, relation count, insight count", inputSchema={"type": "object", "properties": {}}),
        Tool(name="search_chunks", description="Search document chunks using hybrid FTS5 search", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "top_n": {"type": "integer", "default": 5, "description": "Number of results"}}}),
        Tool(name="search_entities", description="Search entities by name (fuzzy match + aliases)", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Entity name to search"}, "entity_type": {"type": "string", "description": "Optional entity type filter"}}}),
        Tool(name="search_insights", description="Search insights by title and content", inputSchema={"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "top_n": {"type": "integer", "default": 5, "description": "Number of results"}}}),
        Tool(name="doc_get", description="Get document chunk content by chunk ID", inputSchema={"type": "object", "properties": {"chunk_id": {"type": "integer", "description": "Chunk ID to retrieve"}}}),
        Tool(name="relation_set", description="Create or update a relation between two entities", inputSchema={"type": "object", "properties": {"entity_a": {"type": "string"}, "entity_b": {"type": "string"}, "relation_type": {"type": "string"}, "weight": {"type": "number", "default": 1.0}, "source_docs": {"type": "array", "items": {"type": "string"}, "description": "Optional source document titles"}}}),
        Tool(name="relation_list", description="List all relations for an entity", inputSchema={"type": "object", "properties": {"entity_name": {"type": "string"}}}),
        Tool(name="graph_navigate", description="Navigate from an entity: single-step expansion, returns anchored and exploration edges", inputSchema={"type": "object", "properties": {"seed": {"type": "string", "description": "Starting entity name"}, "mode": {"type": "string", "enum": ["explore", "focus"], "default": "explore"}}}),
        Tool(name="doc_ingest", description="Ingest a document into L1: accepts .md, .txt, .epub files", inputSchema={"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the document file"}, "title": {"type": "string", "description": "Optional title override"}, "author": {"type": "string", "description": "Optional author"}}}),
        Tool(name="graph_build", description="Build/extend the knowledge graph by extracting entities and relations from ingested documents using LLM", inputSchema={"type": "object", "properties": {"doc_ids": {"type": "array", "items": {"type": "integer"}, "description": "Optional list of doc IDs to process. If empty, processes all docs."}}}),
        Tool(name="entity_register", description="Manually register an entity in the knowledge graph", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Canonical entity name"}, "entity_type": {"type": "string", "description": "Entity type"}, "aliases": {"type": "array", "items": {"type": "string"}, "description": "Optional aliases"}, "description": {"type": "string", "description": "Optional description"}}}),
        Tool(name="insight_record", description="Record an insight/note with optional source docs and entities", inputSchema={"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}, "source_docs": {"type": "array", "items": {"type": "string"}, "description": "Optional list of doc titles"}}}),
        Tool(name="insight_list", description="List recent insights", inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    config = get_config()
    db_path = get_db_path(config.data_dir)
    db = get_db(db_path)

    try:
        if name == "graph_stats":
            return await _graph_stats(db)
        elif name == "search_chunks":
            return await _search_chunks(db, arguments)
        elif name == "search_entities":
            return await _search_entities(db, arguments)
        elif name == "search_insights":
            return await _search_insights(db, arguments)
        elif name == "doc_ingest":
            return await _doc_ingest(db, config, arguments)
        elif name == "doc_get":
            return await _doc_get(db, arguments)
        elif name == "graph_build":
            return await _graph_build(db, config, arguments)
        elif name == "entity_register":
            return await _entity_register(db, arguments)
        elif name == "relation_set":
            return await _relation_set(db, arguments)
        elif name == "relation_list":
            return await _relation_list(db, arguments)
        elif name == "graph_navigate":
            return await _graph_navigate(db, config, arguments)
        elif name == "insight_record":
            return await _insight_record(db, config, arguments)
        elif name == "insight_list":
            return await _insight_list(db, arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# === Tool implementations ===

async def _doc_ingest(db, config: DomainConfig, args) -> list[TextContent]:
    file_path = os.path.expanduser(args["file_path"])
    title_override = args.get("title", "")
    author_override = args.get("author", "")

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

    # Vector indexing (if embedding provider configured)
    vec_count = 0
    emb_provider = create_embedding_provider(config.embedding)
    if emb_provider:
        try:
            vec_count = await index_vectors(db, emb_provider, chunk_ids)
        except Exception as e:
            # Vector indexing failure is non-fatal — doc ingested, just no vectors
            pass

    db.commit()

    return [TextContent(type="text", text=_json_result({
        "ok": True,
        "doc_id": doc_id,
        "title": title,
        "author": author,
        "chunks": chunk_count,
        "vectors": vec_count,
    }))]


async def _graph_build(db, config: DomainConfig, args) -> list[TextContent]:
    doc_ids = args.get("doc_ids", [])
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
    rows = db.execute(
        "SELECT i.id, i.slug, i.title, i.content, i.created_at FROM insights i "
        "JOIN insights_fts f ON i.rowid = f.rowid "
        "WHERE insights_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, top_n)
    ).fetchall()
    results = [{"id": r[0], "slug": r[1], "title": r[2],
                "content": r[3][:300], "created_at": r[4]} for r in rows]
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


async def _relation_set(db, args) -> list[TextContent]:
    source_docs = json.dumps(args.get("source_docs", []), ensure_ascii=False)
    db.execute(
        "INSERT OR REPLACE INTO relations (entity_a, entity_b, relation_type, weight, source_docs_json, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (args["entity_a"], args["entity_b"], args["relation_type"], args.get("weight", 1.0), source_docs)
    )
    db.execute("INSERT OR IGNORE INTO entity_traces (entity_name, source, last_seen) VALUES (?, 'relation_set', datetime('now'))", (args["entity_a"],))
    db.execute("INSERT OR IGNORE INTO entity_traces (entity_name, source, last_seen) VALUES (?, 'relation_set', datetime('now'))", (args["entity_b"],))
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
    mode = args.get("mode", "explore")

    # Resolve seed
    row = db.execute("SELECT canonical_name, entity_type FROM entities WHERE canonical_name = ?", (seed,)).fetchone()
    if not row:
        row = db.execute("SELECT canonical_name, entity_type FROM entities WHERE canonical_name LIKE ? LIMIT 1", (f"%{seed}%",)).fetchone()
    if not row:
        return [TextContent(type="text", text=f"Entity not found: {seed}")]

    name, etype = row

    # Get edges sorted by weight
    edges = db.execute(
        "SELECT entity_a, entity_b, relation_type, weight FROM relations "
        "WHERE entity_a = ? OR entity_b = ? ORDER BY weight DESC",
        (name, name)
    ).fetchall()

    # Classify into anchored vs exploration
    rel_weights = config.relation_types
    anchored = []
    exploration = []

    for a, b, rtype, weight in edges:
        neighbor = b if a == name else a
        entry = {"neighbor": neighbor, "relation": rtype, "weight": weight}

        # Check if neighbor is a known entity (has entries in entities table)
        has_entry = db.execute("SELECT 1 FROM entities WHERE canonical_name = ?", (neighbor,)).fetchone()

        if has_entry and weight >= 1.0:
            anchored.append(entry)
        else:
            exploration.append(entry)

    # Record trace
    db.execute(
        "INSERT INTO entity_traces (entity_name, source, count, last_seen) "
        "VALUES (?, 'navigate', 1, datetime('now')) "
        "ON CONFLICT(entity_name, source) DO UPDATE SET count = count + 1, last_seen = datetime('now')",
        (name,)
    )
    db.commit()

    return [TextContent(type="text", text=_json_result({
        "position": {"entity": name, "type": etype},
        "anchored": anchored[:15],
        "exploration": exploration[:20],
        "is_end": len(anchored) == 0 and len(exploration) == 0
    }))]


def _record_query_history(db, query: str, tool_name: str, results: list[dict]):
    """Record search footprint for interest graph."""
    db.execute(
        "INSERT INTO query_history (query_text, tool_name, top_results_json) VALUES (?, ?, ?)",
        (query, tool_name, json.dumps(results, ensure_ascii=False))
    )
    db.commit()


async def _insight_record(db, config: DomainConfig, args) -> list[TextContent]:
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
    # Sync to FTS5 index
    db.execute(
        "INSERT OR REPLACE INTO insights_fts(rowid, title, content) VALUES (?, ?, ?)",
        (insight_id, title, content)
    )

    # Reverse extract entities and link to L2
    link_result = {}
    try:
        llm = create_llm_provider(config.llm)
        entities, relations = await reverse_extract(llm, config, content)
        link_result = link_insight_to_entities(db, config, insight_id, entities, relations)
    except Exception as e:
        link_result = {"error": str(e)}

    db.commit()
    return [TextContent(type="text", text=_json_result({
        "ok": True,
        "slug": slug,
        "insight_id": insight_id,
        "linked": link_result,
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

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
