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
        Tool(name="entity_get", description="Get entity details and its relations", inputSchema={"type": "object", "properties": {"name": {"type": "string", "description": "Entity canonical name"}}}),
        Tool(name="relation_set", description="Create or update a relation between two entities", inputSchema={"type": "object", "properties": {"entity_a": {"type": "string"}, "entity_b": {"type": "string"}, "relation_type": {"type": "string"}, "weight": {"type": "number", "default": 1.0}}}),
        Tool(name="relation_list", description="List all relations for an entity", inputSchema={"type": "object", "properties": {"entity_name": {"type": "string"}}}),
        Tool(name="graph_navigate", description="Navigate from an entity: single-step expansion, returns anchored and exploration edges", inputSchema={"type": "object", "properties": {"seed": {"type": "string", "description": "Starting entity name"}, "mode": {"type": "string", "enum": ["explore", "focus"], "default": "explore"}}}),
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
        elif name == "doc_get":
            return await _doc_get(db, arguments)
        elif name == "entity_get":
            return await _entity_get(db, arguments)
        elif name == "relation_set":
            return await _relation_set(db, arguments)
        elif name == "relation_list":
            return await _relation_list(db, arguments)
        elif name == "graph_navigate":
            return await _graph_navigate(db, config, arguments)
        elif name == "insight_record":
            return await _insight_record(db, arguments)
        elif name == "insight_list":
            return await _insight_list(db, arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# === Tool implementations ===

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
    # ponytail: LIKE-based search for MVP; upgrade path to FTS5+vector
    # when embedding provider is configured (config.embedding.driver != none)
    rows = db.execute(
        "SELECT c.id, c.doc_id, c.section_path, c.body, d.title "
        "FROM chunks c JOIN docs d ON c.doc_id = d.id "
        "WHERE c.body LIKE ? ORDER BY c.id LIMIT ?",
        (f"%{query}%", top_n)
    ).fetchall()
    results = [{"chunk_id": r[0], "doc_id": r[1], "section": r[2],
                "body": r[3][:500], "doc_title": r[4]} for r in rows]
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
    return [TextContent(type="text", text=_json_result({"results": results, "count": len(results)}))]


async def _search_insights(db, args) -> list[TextContent]:
    query = args["query"]
    top_n = args.get("top_n", 5)
    # ponytail: LIKE-based for MVP; upgrade path to FTS5+vector
    rows = db.execute(
        "SELECT id, slug, title, content, created_at FROM insights "
        "WHERE title LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", f"%{query}%", top_n)
    ).fetchall()
    results = [{"id": r[0], "slug": r[1], "title": r[2],
                "content": r[3][:300], "created_at": r[4]} for r in rows]
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


async def _entity_get(db, args) -> list[TextContent]:
    name = args["name"]
    entity = db.execute(
        "SELECT canonical_name, entity_type, aliases_json, description, source, cross_doc_count "
        "FROM entities WHERE canonical_name = ?", (name,)
    ).fetchone()
    if not entity:
        return [TextContent(type="text", text=f"Entity not found: {name}")]

    relations = db.execute(
        "SELECT entity_a, entity_b, relation_type, weight FROM relations "
        "WHERE entity_a = ? OR entity_b = ? ORDER BY weight DESC",
        (name, name)
    ).fetchall()

    summary = db.execute(
        "SELECT summary FROM entity_summaries WHERE entity_name = ?", (name,)
    ).fetchone()

    return [TextContent(type="text", text=_json_result({
        "name": entity[0], "type": entity[1], "aliases": json.loads(entity[2]),
        "description": entity[3], "source": entity[4], "cross_doc_count": entity[5],
        "summary": summary[0] if summary else "",
        "relations": [{"a": r[0], "b": r[1], "type": r[2], "weight": r[3]} for r in relations]
    }))]


async def _relation_set(db, args) -> list[TextContent]:
    db.execute(
        "INSERT OR REPLACE INTO relations (entity_a, entity_b, relation_type, weight, last_seen) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (args["entity_a"], args["entity_b"], args["relation_type"], args.get("weight", 1.0))
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


async def _insight_record(db, args) -> list[TextContent]:
    import re
    title = args["title"]
    content = args["content"]
    source_docs = args.get("source_docs", [])
    slug = re.sub(r'[^a-z0-9一-鿿]+', '-', title.lower().strip())[:80]

    db.execute(
        "INSERT OR REPLACE INTO insights (slug, title, content, source_docs_json, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (slug, title, content, json.dumps(source_docs, ensure_ascii=False))
    )
    db.commit()
    return [TextContent(type="text", text=_json_result({"ok": True, "slug": slug}))]


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
