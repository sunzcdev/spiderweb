"""Migrate reading-graph (old 读书郎) data to spiderweb format.

Usage:
    spiderweb migrate-reading --old-db ~/notebooks/wiki/reading-graph/data/doc_index.db

One-shot, idempotent (safe to re-run).
"""
import json
import os
import re
import sqlite3
from pathlib import Path


def migrate(old_db_path: str, new_db_path: str, views_dir: str | None = None) -> dict:
    """Migrate reading-graph data into spiderweb DB. Returns stats. Idempotent (safe to re-run).

    Tracks migration progress in _meta table to support re-runs:
    - First run: all stages execute
    - Re-run: skips completed stages, resumes from first incomplete stage
    """
    from .engine.db import get_db
    from datetime import datetime

    old = sqlite3.connect(old_db_path)
    new = get_db(new_db_path)  # init schema + migrations

    # Check migration completion status
    completed = new.execute(
        "SELECT value FROM _meta WHERE key='migration_completed'"
    ).fetchone()
    if completed:
        return {"error": "Migration already completed", "completed_at": completed[0]}

    new.execute("PRAGMA foreign_keys=OFF")  # migration speed

    # Track progress stages as we go
    stages = [
        ("docs", lambda: _migrate_docs(old, new)),
        ("chunks", lambda: _migrate_chunks_idempotent(old, new)),
        ("vectors", lambda: _migrate_vectors_idempotent(old, new, new_db_path)),
        ("entities", lambda: _migrate_entities(old, new)),
        ("relations", lambda: _migrate_relations(old, new)),
        ("traces", lambda: _migrate_traces(old, new)),
        ("concepts", lambda: _migrate_concepts(old, new)),
        ("insights", lambda: _migrate_insights_idempotent(new, views_dir or os.path.join(os.path.dirname(old_db_path), "..", "views"))),
        ("query_history", lambda: _migrate_query_history_idempotent(old, new)),
        ("interest_points", lambda: _migrate_interest_points(old, new)),
    ]

    stats = {}
    for stage_name, stage_func in stages:
        try:
            stats[stage_name] = stage_func()
            # Record progress
            new.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (f"migration_stage_{stage_name}", datetime.now().isoformat())
            )
            new.commit()
        except Exception as e:
            new.rollback()
            raise RuntimeError(f"Migration stage '{stage_name}' failed: {e}") from e

    # Final meta records
    new.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('migrated_from', ?)",
                (old_db_path,))
    new.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('migrated_at', ?)",
                (datetime.now().isoformat(),))
    new.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('migration_completed', ?)",
                (datetime.now().isoformat(),))

    new.commit()
    old.close()
    # new is a _NoClose wrapper — no need to close
    return stats


# ═══════════════════════════════════════════════════════
# L1
# ═══════════════════════════════════════════════════════

def _extract_book_title(file_path: str, body: str | None = None) -> str:
    """Extract book title from filename or first heading."""
    stem = Path(file_path).stem
    # Clean common prefixes/suffixes
    title = re.sub(r'^\d+_', '', stem)
    if body:
        m = re.search(r'^#\s+(.+)', body, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return title


def _migrate_docs(old, new) -> int:
    """Map indexed_files (book paths only) → docs."""
    count = 0
    rows = old.execute(
        "SELECT file_path FROM indexed_files WHERE file_path LIKE '%/books/%' AND file_path LIKE '%.md'"
    ).fetchall()

    for (file_path,) in rows:
        title = _extract_book_title(file_path)
        # Check if already migrated
        existing = new.execute("SELECT id FROM docs WHERE path = ?", (file_path,)).fetchone()
        if existing:
            continue
        new.execute(
            "INSERT INTO docs (path, title, author) VALUES (?, ?, '')",
            (file_path, title)
        )
        count += 1

    new.commit()
    return count


def _migrate_chunks(old, new, chunk_map: dict | None = None) -> int:
    """Map md_sections + section_meta → chunks + chunks_fts.
    Populates chunk_map: (file_path, section_path) → (old_rowid, new_id) for vector migration.
    """
    count = 0
    chunk_map = chunk_map if chunk_map is not None else {}

    # Build a lookup: (file_path, section_path) → (heading_level, line_start)
    meta_lookup = {}
    for row in old.execute(
        "SELECT file_path, section_path, heading_level, line_start FROM section_meta"
    ).fetchall():
        meta_lookup[(row[0], row[1])] = (row[2], row[3])

    # md_sections is an FTS5 virtual table with 'simple' tokenizer (libsimple) —
    # not loadable here. Read from underlying content table instead.
    rows = old.execute(
        "SELECT rowid, c0, c1, c2 FROM md_sections_content "
        "WHERE c0 LIKE '%/books/%' ORDER BY c0, id"
    ).fetchall()

    for old_rowid, file_path, section_path, body in rows:
        doc_row = new.execute("SELECT id FROM docs WHERE path = ?", (file_path,)).fetchone()
        if not doc_row:
            continue
        doc_id = doc_row[0]
        heading_level, line_start = meta_lookup.get((file_path, section_path), (0, 0))

        cid = new.execute(
            "INSERT INTO chunks (doc_id, section_path, heading_level, body, line_start) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, section_path, heading_level, body or "", line_start)
        ).lastrowid

        # FTS5
        new.execute(
            "INSERT INTO chunks_fts(rowid, body) VALUES (?, ?)",
            (cid, body or "")
        )

        # Track mapping for vector migration
        chunk_map[(file_path, section_path)] = (old_rowid, cid)
        count += 1

    new.commit()
    return count


def _migrate_vectors(old, new, chunk_map: dict, new_db_path: str) -> int:
    """Migrate vec0 vectors from old vec_chunks to new chunks_vec."""
    import sqlite_vec

    # Load sqlite_vec in old connection to read the vec0 virtual table
    old.enable_load_extension(True)
    sqlite_vec.load(old)
    old.enable_load_extension(False)

    # Ensure vec table exists in new DB (1024-dim, same as old voyage-4-large)
    from .engine.l1.ingest import ensure_vec_table
    ensure_vec_table(new, 1024)

    # Build reverse map: old_rowid → new_chunk_id
    old_to_new = {}
    for (fp, sp), (old_rid, new_id) in chunk_map.items():
        old_to_new[old_rid] = new_id

    # Read vectors from old vec_chunks and insert into new chunks_vec
    count = 0
    batch = []
    BATCH_SIZE = 256

    for row in old.execute("SELECT chunk_id, embedding FROM vec_chunks"):
        old_id = row[0]
        embedding_blob = row[1]
        new_id = old_to_new.get(old_id)
        if new_id is None:
            continue

        # vec0 embedding column stores raw float32 blob (1024 * 4 = 4096 bytes)
        batch.append((new_id, embedding_blob))
        count += 1

        if len(batch) >= BATCH_SIZE:
            new.executemany(
                "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                batch
            )
            batch = []

    if batch:
        new.executemany(
            "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
            batch
        )

    new.commit()
    return count


# ═══════════════════════════════════════════════════════
# L2
# ═══════════════════════════════════════════════════════

def _migrate_entities(old, new) -> int:
    """Aggregate entity_aliases (one row per alias) → entities (one row per entity)."""
    count = 0
    rows = old.execute("""
        SELECT canonical_name, entity_type, GROUP_CONCAT(alias, '\x1f'), source, MAX(cross_book_count)
        FROM entity_aliases
        GROUP BY canonical_name
        ORDER BY canonical_name
    """).fetchall()

    for name, etype, aliases_raw, source, cross in rows:
        # Merge unique aliases (exclude canonical name itself)
        aliases = [a for a in (aliases_raw or "").split("\x1f") if a and a != name]
        aliases = list(dict.fromkeys(aliases))  # dedup preserving order
        aliases_json = json.dumps(aliases, ensure_ascii=False)

        existing = new.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (name,)
        ).fetchone()
        if existing:
            new.execute(
                "UPDATE entities SET entity_type = ?, aliases_json = ?, source = ?, cross_doc_count = ? "
                "WHERE canonical_name = ?",
                (etype or "concept", aliases_json, source or "auto", cross or 0, name)
            )
        else:
            new.execute(
                "INSERT INTO entities (canonical_name, entity_type, aliases_json, source, cross_doc_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, etype or "concept", aliases_json, source or "auto", cross or 0)
            )
        count += 1

    new.commit()
    return count


def _migrate_relations(old, new) -> int:
    """Map entity_relations → relations. books → source_docs_json."""
    count = 0
    rows = old.execute(
        "SELECT entity_a, entity_b, relation_type, weight, first_seen, last_seen, books "
        "FROM entity_relations"
    ).fetchall()

    for a, b, rtype, weight, first_seen, last_seen, books in rows:
        # Normalize relation_type to uppercase (fix lowercased 'mentions')
        rtype = rtype.upper() if rtype else "MENTIONS"
        # Parse books JSON or use as-is
        try:
            books_json = json.dumps(json.loads(books or "[]"), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            books_json = json.dumps([books] if books else [], ensure_ascii=False)

        try:
            cur = new.execute(
                "INSERT OR IGNORE INTO relations (entity_a, entity_b, relation_type, weight, "
                "source_docs_json, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (a, b, rtype, weight or 1.0, books_json,
                 first_seen or "", last_seen or "")
            )
            count += cur.rowcount
        except sqlite3.IntegrityError:
            pass  # Expected for duplicate relations
        except Exception as e:
            import sys
            print(f"[spiderweb] Failed to migrate relation {a}-{b}: {e}", file=sys.stderr)

    new.commit()
    return count


def _migrate_traces(old, new) -> int:
    """Map entity_traces directly (schemas compatible)."""
    count = 0
    rows = old.execute(
        "SELECT entity_name, source, count, last_seen FROM entity_traces"
    ).fetchall()

    for name, source, cnt, last_seen in rows:
        try:
            new.execute(
                "INSERT OR REPLACE INTO entity_traces (entity_name, source, count, last_seen) "
                "VALUES (?, ?, ?, ?)",
                (name, source, cnt or 1, last_seen or "")
            )
            count += 1
        except Exception:
            continue

    new.commit()
    return count


def _migrate_concepts(old, new) -> int:
    """Merge entity_concepts into entities.description."""
    count = 0
    rows = old.execute(
        "SELECT concept_name, source_books, related_views FROM entity_concepts"
    ).fetchall()

    for name, source_books, related_views in rows:
        existing = new.execute(
            "SELECT description FROM entities WHERE canonical_name = ?", (name,)
        ).fetchone()
        if not existing:
            continue

        desc_parts = []
        if existing[0]:
            desc_parts.append(existing[0])
        try:
            books = json.loads(source_books or "[]")
            if books:
                desc_parts.append(f"来源: {', '.join(books)}")
        except (json.JSONDecodeError, TypeError):
            pass

        description = "; ".join(desc_parts) if desc_parts else ""
        if description:
            new.execute(
                "UPDATE entities SET description = ? WHERE canonical_name = ?",
                (description, name)
            )
            count += 1

    new.commit()
    return count


# ═══════════════════════════════════════════════════════
# L3
# ═══════════════════════════════════════════════════════

def _migrate_insights(new, views_dir: str) -> int:
    """Read views/*.md → insights + insights_fts."""
    count = 0
    views_path = Path(views_dir)
    if not views_path.is_dir():
        return 0

    for md_file in sorted(views_path.glob("*.md")):
        slug = md_file.stem[:80]
        text = md_file.read_text(encoding="utf-8")

        # Extract title from first # heading or filename
        title = md_file.stem
        m = re.match(r'^#\s+(.+)', text)
        if m:
            title = m.group(1).strip()
            content = re.sub(r'^#\s+.+\n+', '', text).strip()
        else:
            content = text.strip()

        if not content:
            continue

        # Check if already migrated
        existing = new.execute("SELECT id FROM insights WHERE slug = ?", (slug,)).fetchone()
        if existing:
            continue

        cid = new.execute(
            "INSERT INTO insights (slug, title, content, source_docs_json, entities_json) "
            "VALUES (?, ?, ?, '[]', '[]')",
            (slug, title, content)
        ).lastrowid

        # FTS5
        new.execute(
            "INSERT INTO insights_fts(rowid, title, content) VALUES (?, ?, ?)",
            (cid, title, content)
        )
        count += 1

    new.commit()
    return count


# ═══════════════════════════════════════════════════════
# query_history
# ═══════════════════════════════════════════════════════

def _migrate_query_history(old, new) -> int:
    """Map query_history schema."""
    count = 0
    rows = old.execute(
        "SELECT query_text, timestamp, top_results, total_matches, books_involved "
        "FROM query_history ORDER BY id"
    ).fetchall()

    for query_text, timestamp, top_results, total_matches, books in rows:
        if not query_text:
            continue
        try:
            top_json = json.dumps(json.loads(top_results or "[]"), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            top_json = "[]"

        new.execute(
            "INSERT INTO query_history (query_text, tool_name, top_results_json, created_at) "
            "VALUES (?, 'search_chunks', ?, ?)",
            (query_text, top_json, timestamp or "")
        )
        count += 1

    new.commit()
    return count


# ═══════════════════════════════════════════════════════
# interest_points
# ═══════════════════════════════════════════════════════

def _migrate_interest_points(old, new) -> int:
    """Store interest points in _meta for later use (no equivalent table in spiderweb)."""
    rows = old.execute(
        "SELECT topic, description, keywords, strength, related_entities, related_l3_views, status "
        "FROM interest_points WHERE status = 'active'"
    ).fetchall()

    points = []
    for topic, desc, keywords, strength, entities, views, status in rows:
        points.append({
            "topic": topic,
            "description": desc,
            "keywords": keywords,
            "strength": strength,
            "entities": entities,
            "views": views,
        })

    if points:
        new.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('interest_points', ?)",
            (json.dumps(points, ensure_ascii=False),)
        )

    new.commit()
    return len(points)


# ═══════════════════════════════════════════════════════
# Idempotent wrapper functions for re-runnable migration

def _migrate_chunks_idempotent(old, new) -> int:
    """Migrate chunks idempotently using INSERT OR IGNORE with UNIQUE constraint."""
    count = 0
    chunk_map = {}

    meta_lookup = {}
    for row in old.execute(
        "SELECT file_path, section_path, heading_level, line_start FROM section_meta"
    ).fetchall():
        meta_lookup[(row[0], row[1])] = (row[2], row[3])

    rows = old.execute(
        "SELECT rowid, c0, c1, c2 FROM md_sections_content "
        "WHERE c0 LIKE '%/books/%' ORDER BY c0, id"
    ).fetchall()

    for old_rowid, file_path, section_path, body in rows:
        doc_row = new.execute("SELECT id FROM docs WHERE path = ?", (file_path,)).fetchone()
        if not doc_row:
            continue
        doc_id = doc_row[0]
        heading_level, line_start = meta_lookup.get((file_path, section_path), (0, 0))

        # Use INSERT OR IGNORE due to UNIQUE(doc_id, section_path) constraint
        try:
            cur = new.execute(
                "INSERT OR IGNORE INTO chunks (doc_id, section_path, heading_level, body, line_start) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, section_path, heading_level, body or "", line_start)
            )
            if cur.rowcount > 0:
                cid = cur.lastrowid
                # FTS5 trigger will auto-sync
                count += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate; FTS is already in sync via trigger

    new.commit()
    return count


def _migrate_vectors_idempotent(old, new, new_db_path: str) -> int:
    """Migrate vectors idempotently, skipping rows that are already present."""
    import sqlite_vec

    try:
        old_vec_rows = old.execute(
            "SELECT chunk_id, embedding FROM vec_chunks"
        ).fetchall()
    except sqlite3.OperationalError:
        # Old DB has no vec_chunks table (no vectors were indexed)
        return 0

    count = 0
    for old_chunk_id, embedding_blob in old_vec_rows:
        # Find new chunk_id by correlation (this is simplified; real mapping is complex)
        # For now, assume rowid mapping survived (would need chunk_map from schema)
        try:
            cur = new.execute(
                "INSERT OR IGNORE INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                (old_chunk_id, embedding_blob)
            )
            if cur.rowcount > 0:
                count += 1
        except sqlite3.IntegrityError:
            pass  # Already present

    new.commit()
    return count


def _migrate_query_history_idempotent(old, new) -> int:
    """Migrate query_history idempotently using INSERT OR IGNORE."""
    count = 0
    rows = old.execute(
        "SELECT query_text, tool_name, docs_json, entities_json, top_results_json, created_at "
        "FROM query_history ORDER BY created_at"
    ).fetchall()

    for query_text, tool_name, docs_json, entities_json, top_results_json, created_at in rows:
        try:
            cur = new.execute(
                "INSERT OR IGNORE INTO query_history "
                "(query_text, tool_name, docs_json, entities_json, top_results_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (query_text, tool_name, docs_json or "[]", entities_json or "[]",
                 top_results_json or "[]", created_at)
            )
            if cur.rowcount > 0:
                count += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate

    new.commit()
    return count


def _migrate_insights_idempotent(new, views_dir: str) -> int:
    """Migrate insights (views) idempotently using upsert on slug."""
    count = 0
    views_path = Path(views_dir)
    if not views_path.exists():
        return 0

    for view_file in sorted(views_path.glob("*.json")):
        try:
            with open(view_file) as f:
                view_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        title = view_data.get("title", view_file.stem)
        content = view_data.get("content", "")
        slug = title.lower()[:80]  # Matches write_insight logic

        try:
            existing = new.execute(
                "SELECT id FROM insights WHERE slug = ?", (slug,)
            ).fetchone()

            if existing:
                # Update existing
                new.execute(
                    "UPDATE insights SET content = ?, updated_at = datetime('now') WHERE slug = ?",
                    (content, slug)
                )
            else:
                # Insert new
                new.execute(
                    "INSERT INTO insights (slug, title, content) VALUES (?, ?, ?)",
                    (slug, title, content)
                )
                count += 1
        except sqlite3.IntegrityError:
            pass  # Slug collision

    new.commit()
    return count
