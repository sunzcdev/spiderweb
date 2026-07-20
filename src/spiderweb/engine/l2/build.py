"""graph_build — orchestrate entity/relation extraction from docs."""
import json
import math
import sqlite3

from ..providers import LLMProvider, create_llm_provider
from ..domain import DomainConfig
from ..hooks import run_post_extract


def sample_chunks(db, doc_ids: list[int], mode: str, sample_rate: float) -> list[tuple[int, str]]:
    """Select chunks for extraction. Returns [(chunk_id, body), ...]."""
    if not doc_ids:
        rows = db.execute("SELECT id, body FROM chunks ORDER BY id").fetchall()
    else:
        placeholders = ",".join("?" * len(doc_ids))
        rows = db.execute(
            f"SELECT id, body FROM chunks WHERE doc_id IN ({placeholders}) ORDER BY id",
            doc_ids
        ).fetchall()

    if not rows:
        return []

    MAX_CHUNKS = 30  # old reading-graph limit — keeps extraction fast

    if mode == "full":
        return rows[:MAX_CHUNKS]

    # Sample mode: sqrt(total_chars) * sample_rate, capped
    total_chars = sum(len(r[1]) for r in rows)
    target = max(10, int(math.sqrt(total_chars) * sample_rate))
    if target >= len(rows):
        return rows[:MAX_CHUNKS]

    # Even sampling
    step = len(rows) / target
    sampled = [rows[int(i * step)] for i in range(target)]
    return sampled[:MAX_CHUNKS]


async def build_graph(
    db,
    config: DomainConfig,
    doc_ids: list[int] | None = None,
    batch_size: int = 3,
) -> dict:
    """Build/extend the knowledge graph. Returns stats dict."""
    llm = create_llm_provider(config.llm)

    mode = config.extraction_mode
    rate = config.extraction_sample_rate
    chunks = sample_chunks(db, doc_ids or [], mode, rate)

    if not chunks:
        return {"entities_found": 0, "relations_added": 0, "chunks_processed": 0}

    from .extract import extract_from_chunks
    entities, relations = await extract_from_chunks(llm, config, chunks, batch_size)

    # Run post_extract hooks (chainable: rule engines, custom enhancers)
    entities, relations = run_post_extract(
        config.hooks_module, entities, relations, str(doc_ids or [])
    )

    # Write entities
    registered = 0
    for e in entities:
        name = e.get("name", "").strip()
        etype = e.get("type", "concept")
        aliases = json.dumps(e.get("aliases", []), ensure_ascii=False)
        description = e.get("description", "")

        existing = db.execute(
            "SELECT id, source_docs_json FROM entities WHERE canonical_name = ?", (name,)
        ).fetchone()

        if existing:
            # Update description if we got new info
            if description:
                db.execute(
                    "UPDATE entities SET description = ?, aliases_json = ? WHERE canonical_name = ?",
                    (description, aliases, name)
                )
            # Accumulate source docs
            if doc_ids:
                existing_docs = set(json.loads(existing[1] or "[]"))
                existing_docs.update(str(d) for d in doc_ids)
                db.execute(
                    "UPDATE entities SET source_docs_json = ? WHERE canonical_name = ?",
                    (json.dumps(list(existing_docs), ensure_ascii=False), name)
                )
        else:
            source_json = json.dumps([str(d) for d in doc_ids], ensure_ascii=False) if doc_ids else '[]'
            db.execute(
                "INSERT INTO entities (canonical_name, entity_type, aliases_json, description, source, source_docs_json) "
                "VALUES (?, ?, ?, ?, 'auto', ?)",
                (name, etype, aliases, description, source_json)
            )
            registered += 1

    # Write relations
    rel_added = 0
    for r in relations:
        a, b, rtype = r["entity_a"], r["entity_b"], r["relation_type"]
        rel_info = config.relation_types.get(rtype, {})
        weight = rel_info.get("weight", 1.0) if isinstance(rel_info, dict) else 1.0

        try:
            cur = db.execute(
                "INSERT OR IGNORE INTO relations (entity_a, entity_b, relation_type, weight) "
                "VALUES (?, ?, ?, ?)",
                (a, b, rtype, weight)
            )
            rel_added += cur.rowcount
        except sqlite3.IntegrityError:
            pass  # Unique constraint violation (expected for duplicates)
        except Exception as e:
            import sys
            print(f"[spiderweb] Failed to insert relation {a}-{b}: {e}", file=sys.stderr)

    db.commit()

    # Update cross_doc_count for affected entities only (not a full scan)
    affected_names = [e.get("name", "").strip() for e in entities if e.get("name")]
    _update_cross_doc_counts(db, affected_names)

    return {
        "entities_found": len(entities),
        "entities_registered": registered,
        "relations_added": rel_added,
        "chunks_processed": len(chunks),
    }


def _update_cross_doc_counts(db, entity_names: list[str] | None = None):
    """Update cross_doc_count using explicit source_docs_json (primary) and LIKE fallback.

    Entities extracted during build_graph now store their source doc_ids in
    source_docs_json, so we count those docs directly. For legacy entities
    (empty source_docs_json), we fall back to LIKE matching on chunk bodies.

    When entity_names is provided, only those entities are updated (partial).
    When None, all entities are updated (full scan).
    """
    if entity_names:
        placeholders = ",".join("?" * len(entity_names))
        rows = db.execute(
            f"SELECT canonical_name, source_docs_json FROM entities "
            f"WHERE canonical_name IN ({placeholders})",
            entity_names
        ).fetchall()
    else:
        rows = db.execute("SELECT canonical_name, source_docs_json FROM entities").fetchall()

    for (name, source_json) in rows:
        source_docs = json.loads(source_json or "[]")
        if source_docs:
            # Count verified source docs (the docs we extracted from)
            placeholders = ",".join("?" * len(source_docs))
            count = db.execute(
                f"SELECT COUNT(DISTINCT id) FROM docs WHERE id IN ({placeholders})",
                [int(d) for d in source_docs]
            ).fetchone()[0]
        else:
            # Fallback: LIKE matching on chunk bodies (legacy entities)
            count = db.execute(
                "SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE body LIKE ?",
                (f"%{name}%",)
            ).fetchone()[0]
        db.execute(
            "UPDATE entities SET cross_doc_count = ? WHERE canonical_name = ?",
            (count, name)
        )
    db.commit()
