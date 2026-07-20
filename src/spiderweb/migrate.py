"""Database migration system.

Philosophy: "No migration — old data stays as-is" (DECISIONS.md).
This module provides opt-in migration scripts that can be applied
manually or automatically on startup via auto_migrate().

Design:
- Migrations are idempotent: safe to run multiple times.
- Uses _meta table to track applied migration names.
- Each migration is a function that takes a db connection and
  returns True if it made changes.
"""

# Ordered list of all migrations (append new ones to the end)
_MIGRATIONS: list[tuple[str, str, "callable"]] = []


def _migration(name: str, description: str):
    """Decorator to register a migration."""
    def decorator(fn):
        _MIGRATIONS.append((name, description, fn))
        return fn
    return decorator


@_migration("add_relations_metadata", "Add metadata column to relations table")
def _add_relations_metadata(db) -> bool:
    cursor = db.execute("PRAGMA table_info(relations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "metadata" not in columns:
        db.execute("ALTER TABLE relations ADD COLUMN metadata TEXT DEFAULT '{}'")
        return True
    return False


@_migration("add_entity_timestamps", "Add created_at and last_accessed_at to entities")
def _add_entity_timestamps(db) -> bool:
    cursor = db.execute("PRAGMA table_info(entities)")
    columns = [row[1] for row in cursor.fetchall()]
    changed = False
    if "created_at" not in columns:
        db.execute("ALTER TABLE entities ADD COLUMN created_at TEXT DEFAULT ''")
        changed = True
    if "last_accessed_at" not in columns:
        db.execute("ALTER TABLE entities ADD COLUMN last_accessed_at TEXT DEFAULT ''")
        changed = True
    return changed


@_migration("drop_entity_summaries", "Remove deprecated entity_summaries table")
def _drop_entity_summaries(db) -> bool:
    """Drop entity_summaries if it exists (removed from SCHEMA in v0.2)."""
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_summaries'"
    ).fetchone()
    if row:
        db.execute("DROP TABLE IF EXISTS entity_summaries")
        return True
    return False


@_migration("ensure_entity_traces", "Ensure entity_traces table exists with correct schema")
def _ensure_entity_traces(db) -> bool:
    """entity_traces table should exist with correct columns."""
    existing = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_traces'"
    ).fetchone()
    if not existing:
        db.execute("""
            CREATE TABLE IF NOT EXISTS entity_traces (
                entity_name TEXT NOT NULL,
                source TEXT NOT NULL,
                count INTEGER DEFAULT 1,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (entity_name, source)
            )
        """)
        return True
    return False


@_migration("rebuild_fts_external_content", "Rebuild FTS tables with external-content mode + triggers")
def _rebuild_fts_external_content(db) -> bool:
    """Upgrade chunks_fts and insights_fts from embedded to external-content mode with triggers.

    Drops old FTS tables (content is in base tables), recreates with external-content,
    rebuilds indexes from base tables, and creates sync triggers.
    """
    # Check if already upgraded: look for content='chunks' in FTS table definition
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if row and "content='chunks'" in row[0]:
        return False  # Already external-content

    # 1. Drop old FTS tables
    db.execute("DROP TABLE IF EXISTS chunks_fts")
    db.execute("DROP TABLE IF EXISTS insights_fts")

    # 2. Recreate with external-content mode
    db.executescript("""
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            body,
            content='chunks',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES('delete', old.id, old.body);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
          INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES('delete', old.id, old.body);
          INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body);
        END;

        CREATE VIRTUAL TABLE insights_fts USING fts5(
            title,
            content,
            content='insights',
            content_rowid='id',
            tokenize='unicode61'
        );

        CREATE TRIGGER IF NOT EXISTS insights_ai AFTER INSERT ON insights BEGIN
          INSERT INTO insights_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS insights_ad AFTER DELETE ON insights BEGIN
          INSERT INTO insights_fts(insights_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS insights_au AFTER UPDATE ON insights BEGIN
          INSERT INTO insights_fts(insights_fts, rowid, title, content) VALUES('delete', old.id, old.title, old.content);
          INSERT INTO insights_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
        END;
    """)

    # 3. Rebuild FTS indexes from base tables
    db.execute("INSERT INTO chunks_fts(rowid, body) SELECT id, body FROM chunks")
    db.execute("INSERT INTO insights_fts(rowid, title, content) SELECT id, title, content FROM insights")

    return True


@_migration("add_entity_source_docs", "Add source_docs_json column for explicit book binding")
def _add_entity_source_docs(db) -> bool:
    """Add source_docs_json column to entities so extracted entities are
    explicitly linked to their source books, not just via LIKE matching."""
    cursor = db.execute("PRAGMA table_info(entities)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source_docs_json" not in columns:
        db.execute("ALTER TABLE entities ADD COLUMN source_docs_json TEXT DEFAULT '[]'")
        return True
    return False


@_migration("add_chunks_unique_index", "Add unique index on chunks(doc_id, section_path)")
def _add_chunks_unique_index(db) -> bool:
    """Add UNIQUE index for idempotent chunk inserts. Can't ALTER TABLE ADD CONSTRAINT
    in SQLite, so we create an equivalent unique index.

    Deduplicates chunks first — old data may have duplicates from pre-fix ingestion.
    Keeps the earliest row (lowest id) for each (doc_id, section_path) pair.
    """
    # Check if index already exists
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_chunks_unique'"
    ).fetchone()
    if row:
        return False

    # Deduplicate: keep lowest id per (doc_id, section_path)
    db.execute("""
        DELETE FROM chunks WHERE id NOT IN (
            SELECT MIN(id) FROM chunks GROUP BY doc_id, section_path
        )
    """)

    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_unique ON chunks(doc_id, section_path)")
    return True


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def init_meta(db):
    """Ensure _meta table exists."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS _meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db.execute(
        "INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0.2')"
    )
    db.commit()


def _is_applied(db, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM _meta WHERE key = ?", (f"migration:{name}",)
    ).fetchone()
    return row is not None


def _mark_applied(db, name: str):
    db.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, datetime('now'))",
        (f"migration:{name}",)
    )
    db.commit()


def auto_migrate(db) -> list[str]:
    """Run all unapplied migrations. Returns names of applied migrations."""
    init_meta(db)
    applied = []
    for name, desc, fn in _MIGRATIONS:
        if not _is_applied(db, name):
            changed = fn(db)
            _mark_applied(db, name)
            applied.append(name)
    return applied


def pending_migrations(db) -> list[tuple[str, str]]:
    """List migrations that have not been applied yet."""
    init_meta(db)
    return [(name, desc) for name, desc, _fn in _MIGRATIONS if not _is_applied(db, name)]


def status(db) -> dict:
    """Return migration status overview."""
    init_meta(db)
    migrations = []
    for name, desc, _fn in _MIGRATIONS:
        migrations.append({
            "name": name,
            "description": desc,
            "applied": _is_applied(db, name),
        })
    return {
        "total": len(_MIGRATIONS),
        "migrations": migrations,
    }
