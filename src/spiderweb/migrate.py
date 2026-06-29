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


# ──────────────────────────────────────────────
# Migrations
# ──────────────────────────────────────────────

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
