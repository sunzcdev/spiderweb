"""L3 insight reverse extraction — connect insights to L2 entities."""
import json
import re
import asyncio
from ..providers import LLMProvider, create_llm_provider
from ..domain import DomainConfig, get_entity_types_flat


async def _backoff(attempt: int):
    """Exponential backoff: 0.5s, 1s, 2s."""
    await asyncio.sleep(0.5 * (2 ** attempt))

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

    async def _try_extract(model_provider):
        for attempt in range(3):
            try:
                response = await model_provider.chat([
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ])
                if not response or not response.strip():
                    continue  # empty response — retry
                data = _parse_json(response)
                if data and "entities" in data:
                    return data.get("entities", []), data.get("relations", [])
            except Exception:
                pass
            await _backoff(attempt)
        return None

    # Try model (OpenAILLM internally handles primary → configured fallbacks)
    result = await _try_extract(llm)
    if result is not None:
        return result

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
    entity_names = []  # list of (name, type) tuples

    for e in entities:
        name = e.get("name", "").strip()
        etype = e.get("type", "concept")
        if not name:
            continue
        entity_names.append((name, etype))

        existing = db.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (name,)
        ).fetchone()

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
        for name, _ in entity_names:
            if _ensure_relation(db, insight_title, name, "MENTIONS", 1.0, config):
                linked += 1

    # Entity co-occurrence within the insight (all pairs mention each other)
    all_rels = list(relations)
    for i, (a, _) in enumerate(entity_names):
        for b, _ in entity_names[i + 1:]:
            all_rels.append({"entity_a": a, "entity_b": b, "relation_type": "MENTIONS"})

    for r in all_rels:
        a, b, rtype = r.get("entity_a", ""), r.get("entity_b", ""), r.get("relation_type", "MENTIONS")
        if a and b:
            if _ensure_relation(db, a, b, rtype, 1.0, config):
                linked += 1

    # Record traces
    for name, _ in entity_names:
        db.execute(
            "INSERT INTO entity_traces (entity_name, source, count, last_seen) "
            "VALUES (?, 'l3_insight', 1, datetime('now')) "
            "ON CONFLICT(entity_name, source) DO UPDATE SET count = count + 1, last_seen = datetime('now')",
            (name,)
        )

    # Update insight's entities_json
    db.execute(
        "UPDATE insights SET entities_json = ? WHERE id = ?",
        (json.dumps([n for n, _ in entity_names], ensure_ascii=False), insight_id)
    )

    # Update cross_doc_count for affected entities
    for name, _ in entity_names:
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
        "entity_names": [n for n, _ in entity_names],
        "entity_types": {n: t for n, t in entity_names},
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


async def write_insight(db, config, title: str, content: str, source_docs: list | None = None) -> dict:
    """Write an insight: DB insert, FTS5, vector, entity extraction + linking.

    Returns dict with slug, insight_id, entity stats, linked_books, vectors.
    This is the engine-level write path — no progress callbacks, no MCP wrapping.
    """
    from ..providers import create_tokenizer_provider, create_embedding_provider
    from ..l1.ingest import ensure_vec_table

    source_docs = source_docs or []
    slug = re.sub(r'[^a-z0-9一-鿿]+', '-', title.lower().strip())[:80]

    cursor = db.execute(
        "INSERT OR REPLACE INTO insights (slug, title, content, source_docs_json, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (slug, title, content, json.dumps(source_docs, ensure_ascii=False))
    )
    insight_id = cursor.lastrowid

    # FTS5: write tokenized text for Chinese word segmentation
    tokenizer = create_tokenizer_provider(config.tokenizer)
    fts_title = tokenizer.tokenize(title) if tokenizer else title
    fts_content = tokenizer.tokenize(content) if tokenizer else content
    db.execute(
        "INSERT OR REPLACE INTO insights_fts(rowid, title, content) VALUES (?, ?, ?)",
        (insight_id, fts_title, fts_content)
    )

    # Vector: embed for semantic search
    emb_provider = create_embedding_provider(config.embedding)
    vec_count = 0
    if emb_provider:
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
    try:
        from ..providers import create_llm_provider
        llm = create_llm_provider(config.llm)
        entities, relations = await reverse_extract(llm, config, content)
        link_result = link_insight_to_entities(db, config, insight_id, entities, relations)
    except Exception as e:
        link_result = {"error": str(e)}

    # Linked books (from source_docs + entity traces)
    linked_books = list(set(source_docs))
    if link_result and "entity_names" in link_result:
        for name in link_result["entity_names"]:
            # Skip generic terms that appear in many docs (use cached cross_doc_count)
            row = db.execute(
                "SELECT cross_doc_count FROM entities WHERE canonical_name = ?", (name,)
            ).fetchone()
            if row and row[0] and row[0] > 5:
                continue
            doc_rows = db.execute(
                "SELECT DISTINCT d.title FROM chunks c JOIN docs d ON c.doc_id = d.id "
                "WHERE c.body LIKE ? LIMIT 3", (f"%{name}%",)
            ).fetchall()
            for (dt,) in doc_rows:
                if dt not in linked_books:
                    linked_books.append(dt)

    db.commit()
    return {
        "ok": True,
        "slug": slug,
        "insight_id": insight_id,
        "entities_extracted": link_result.get("entities_extracted", 0),
        "entities_registered": link_result.get("entities_registered", 0),
        "edges_added": link_result.get("edges_added", 0),
        "linked_books": linked_books[:10],
        "entity_names": link_result.get("entity_names", []),
        "vectors": vec_count,
    }


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
