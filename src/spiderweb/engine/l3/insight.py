"""L3 insight reverse extraction — connect insights to L2 entities."""
import json
import re
from ..providers import LLMProvider, create_llm_provider
from ..domain import DomainConfig, get_entity_types_flat

REVERSE_EXTRACT_SYSTEM = """You extract entities and relations from a user's personal note/insight.
Output ONLY valid JSON.

Given a note, extract:
1. entities: list of {{"name": "canonical name", "type": "entity_type"}}
2. relations: list of {{"entity_a": "name", "entity_b": "name", "relation_type": "MENTIONS"}}

Rules:
- Entity types must be from the provided list
- Only extract entities EXPLICITLY mentioned in the note
- Use the most standard canonical form for names
- All relations should use "MENTIONS" type (the note mentions these entities together)
- Include entities that are the SUBJECT of the note, not just passing mentions"""

REVERSE_EXTRACT_USER = """Entity types available:
{entity_types}

User's note:
---
{content}
---

Output JSON with "entities" and "relations" arrays.
{{"entities": [...], "relations": [...]}}"""


async def reverse_extract(
    llm: LLMProvider,
    config: DomainConfig,
    content: str,
) -> tuple[list[dict], list[dict]]:
    """Extract entities and co-occurrence relations from an insight."""
    from ..domain import load_prompt

    entity_types_str = ", ".join(sorted(get_entity_types_flat(config)))

    prompt = REVERSE_EXTRACT_USER.format(
        entity_types=entity_types_str,
        content=content,
    )

    sys_prompt = load_prompt(config, "record_insight.md") or REVERSE_EXTRACT_SYSTEM

    for attempt in range(3):
        try:
            response = await llm.chat([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ])
            data = _parse_json(response)
            if data and "entities" in data:
                return data.get("entities", []), data.get("relations", [])
        except Exception:
            if attempt == 2:
                raise
            continue

    return [], []


def link_insight_to_entities(
    db,
    config: DomainConfig,
    insight_id: int,
    entities: list[dict],
    relations: list[dict],
) -> dict:
    """Register extracted entities, build MENTIONS edges, update traces. Returns stats."""
    registered = 0
    linked = 0
    entity_names = []

    for e in entities:
        name = e.get("name", "").strip()
        etype = e.get("type", "concept")
        if not name:
            continue
        entity_names.append(name)

        existing = db.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (name,)
        ).fetchone()

        if not existing:
            db.execute(
                "INSERT INTO entities (canonical_name, entity_type, source, aliases_json) "
                "VALUES (?, ?, 'l3_insight', '[]')",
                (name, etype)
            )
            registered += 1

    # Build MENTIONS edges: insight → each entity, entity-to-entity within the insight
    insight_slug = db.execute("SELECT slug FROM insights WHERE id = ?", (insight_id,)).fetchone()
    if insight_slug:
        insight_title = insight_slug[0]
        # Insight mentions each entity
        for name in entity_names:
            if _ensure_relation(db, insight_title, name, "MENTIONS", 1.0, config):
                linked += 1

    # Entity co-occurrence within the insight (all pairs mention each other)
    all_rels = list(relations)
    for i, a in enumerate(entity_names):
        for b in entity_names[i + 1:]:
            all_rels.append({"entity_a": a, "entity_b": b, "relation_type": "MENTIONS"})

    for r in all_rels:
        a, b, rtype = r.get("entity_a", ""), r.get("entity_b", ""), r.get("relation_type", "MENTIONS")
        if a and b:
            if _ensure_relation(db, a, b, rtype, 1.0, config):
                linked += 1

    # Record traces
    for name in entity_names:
        db.execute(
            "INSERT INTO entity_traces (entity_name, source, count, last_seen) "
            "VALUES (?, 'l3_insight', 1, datetime('now')) "
            "ON CONFLICT(entity_name, source) DO UPDATE SET count = count + 1, last_seen = datetime('now')",
            (name,)
        )

    # Update insight's entities_json
    db.execute(
        "UPDATE insights SET entities_json = ? WHERE id = ?",
        (json.dumps(entity_names, ensure_ascii=False), insight_id)
    )

    # Update cross_doc_count for affected entities
    for name in entity_names:
        count = db.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE body LIKE ?",
            (f"%{name}%",)
        ).fetchone()[0]
        db.execute(
            "UPDATE entities SET cross_doc_count = ? WHERE canonical_name = ?",
            (count, name)
        )

    db.commit()

    return {
        "entities_extracted": len(entity_names),
        "entities_registered": registered,
        "edges_added": linked,
        "entity_names": entity_names,
    }


def _ensure_relation(db, a: str, b: str, rtype: str, weight: float, config: DomainConfig) -> bool:
    """Insert relation if it doesn't exist. Returns True if added."""
    if a == b:
        return False
    existing = db.execute(
        "SELECT id FROM relations WHERE entity_a = ? AND entity_b = ? AND relation_type = ?",
        (a, b, rtype)
    ).fetchone()
    if existing:
        return False
    db.execute(
        "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?, ?, ?, ?)",
        (a, b, rtype, weight)
    )
    return True


def _parse_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
