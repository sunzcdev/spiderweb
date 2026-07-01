"""L2 search — entities, graph navigation, stats."""
import json


def search_entities(db, query: str, entity_type: str = "") -> list[dict]:
    """Search entities by name (fuzzy LIKE + aliases)."""
    sql = "SELECT canonical_name, entity_type, aliases_json, description, cross_doc_count FROM entities WHERE (canonical_name LIKE ? OR aliases_json LIKE ?)"
    params = [f"%{query}%", f"%{query}%"]
    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    rows = db.execute(sql, params).fetchall()
    return [{
        "name": r[0], "type": r[1],
        "aliases": json.loads(r[2]) if r[2] else [],
        "description": r[3], "cross_doc_count": r[4],
    } for r in rows]


def entity_get(db, name: str) -> dict | None:
    """Get entity detail: type, aliases, relations, insights, books, footprint."""
    row = db.execute(
        "SELECT canonical_name, entity_type, aliases_json, description, source, cross_doc_count "
        "FROM entities WHERE canonical_name = ?", (name.strip(),)
    ).fetchone()
    if not row:
        return None

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

    # Related insights (via entities_json LIKE)
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

    return {
        "name": row[0], "type": row[1],
        "aliases": json.loads(row[2] or "[]"),
        "description": row[3], "source": row[4],
        "cross_doc_count": row[5],
        "books": book_list,
        "key_relations": key_relations,
        "related_insights": related_insights,
        "footprint": footprint,
    }


def graph_navigate(db, seed: str, depth: int = 1) -> dict | None:
    """Navigate knowledge graph from a seed entity.

    Returns positioned entity with anchored edges and exploration edges,
    or None if seed not found.
    """
    # Resolve seed
    row = db.execute(
        "SELECT canonical_name, entity_type FROM entities WHERE canonical_name = ?", (seed,)
    ).fetchone()
    if not row:
        row = db.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE canonical_name LIKE ? LIMIT 1",
            (f"%{seed}%",)
        ).fetchone()
    if not row:
        return None

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
            has_entry = db.execute(
                "SELECT 1 FROM entities WHERE canonical_name = ?", (neighbor,)
            ).fetchone()
            if has_entry and weight >= 1.0:
                anc.append(entry)
            else:
                exp.append(entry)
        return anc, exp

    anchored, exploration = _expand(name)

    # Depth=2: treat top anchored neighbor as next hop
    if depth >= 2 and anchored:
        depth1_anchored = anchored[:15]
        depth1_exploration = exploration[:10]
        next_hop = anchored[0]["neighbor"]
        path_trace.append(next_hop)
        anc2, exp2 = _expand(next_hop)
        anchored = depth1_anchored + [
            {"neighbor": n["neighbor"], "relation": f"{name}→{next_hop}→", "weight": n["weight"]}
            for n in anc2[:10]
        ]
        exploration = depth1_exploration + exp2[:10]

    # Record footprints
    _record_trace(db, name)
    for entry in anchored[:15] + exploration[:20]:
        _record_trace(db, entry["neighbor"])

    return {
        "position": {"entity": name, "type": etype},
        "anchored": anchored[:15],
        "exploration": exploration[:20],
        "path_trace": path_trace,
        "is_end": len(anchored) == 0 and len(exploration) == 0,
    }


def graph_stats(db) -> dict:
    """Get graph statistics: counts and relation type breakdown."""
    stats = {}
    for table, label in [("docs", "docs"), ("entities", "entities"),
                          ("relations", "relations"), ("insights", "insights")]:
        row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        stats[label] = row[0]
    types = db.execute(
        "SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type"
    ).fetchall()
    stats["relation_types"] = {t: c for t, c in types}
    return stats


def _record_trace(db, entity_name):
    """Record one footprint for an entity."""
    db.execute(
        "INSERT INTO entity_traces (entity_name, source, count, last_seen) "
        "VALUES (?, 'navigate', 1, datetime('now')) "
        "ON CONFLICT(entity_name, source) DO UPDATE SET count = count + 1, last_seen = datetime('now')",
        (entity_name,)
    )
