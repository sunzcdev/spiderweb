"""SQLite schema and connection management."""
import sqlite3
import sqlite_vec
import os
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT DEFAULT '',
    meta_json TEXT DEFAULT '{}',
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    section_path TEXT DEFAULT '',
    heading_level INTEGER DEFAULT 0,
    body TEXT NOT NULL,
    line_start INTEGER DEFAULT 0,
    UNIQUE(doc_id, section_path)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    body,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);

-- Triggers to keep chunks_fts in sync with chunks
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

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL DEFAULT 'concept',
    aliases_json TEXT DEFAULT '[]',
    description TEXT DEFAULT '',
    source TEXT DEFAULT 'auto',
    cross_doc_count INTEGER DEFAULT 0,
    source_docs_json TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a TEXT NOT NULL,
    entity_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    source_docs_json TEXT DEFAULT '[]',
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_a, entity_b, relation_type)
);

CREATE TABLE IF NOT EXISTS entity_traces (
    entity_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'navigate',
    count INTEGER DEFAULT 1,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_name, source)
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_docs_json TEXT DEFAULT '[]',
    entities_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
    title,
    content,
    content='insights',
    content_rowid='id',
    tokenize='unicode61'
);

-- Triggers to keep insights_fts in sync with insights
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

CREATE TABLE IF NOT EXISTS query_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_text TEXT NOT NULL,
    tool_name TEXT DEFAULT '',
    docs_json TEXT DEFAULT '[]',
    entities_json TEXT DEFAULT '[]',
    top_results_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_relations_a ON relations(entity_a);
CREATE INDEX IF NOT EXISTS idx_relations_b ON relations(entity_b);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_traces_entity ON entity_traces(entity_name);
"""


class _NoClose:
    """Wrapper that makes close() a no-op — shared module-level connection."""
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn.__enter__() or self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._conn.rollback()  # safety rollback, not real close
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def really_close(self):
        self._conn.close()


_conn = None


def get_db(db_path: str) -> _NoClose:
    """Get or create the shared database connection.

    This function relies on SQLite's native WAL recovery mechanism.
    WAL files are never manually deleted — SQLite handles them correctly
    on connection open. We only need to ensure integrity after recovery.
    """
    global _conn
    if _conn is not None:
        return _conn

    dir_path = os.path.dirname(db_path) or "."
    os.makedirs(dir_path, exist_ok=True)

    # Let SQLite handle WAL recovery on open (it does this correctly)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=500")  # ~2MB before auto-checkpoint
    conn.execute("PRAGMA foreign_keys=ON")

    # After open, perform checkpoint and integrity check to ensure clean state
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity_result = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity_result and integrity_result[0] != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity_result[0]}")
    except sqlite3.DatabaseError as e:
        conn.close()
        raise RuntimeError(f"Failed to verify database integrity: {e}") from e

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA)
    conn.commit()

    # Run pending migrations (idempotent, safe to run every startup)
    from ..migrate import auto_migrate
    applied = auto_migrate(conn)
    if applied:
        conn.commit()

    _conn = _NoClose(conn)
    return _conn


def get_db_path(data_dir: str) -> str:
    return os.path.join(data_dir, "spiderweb.db")
