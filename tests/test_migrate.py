"""Test database migrations — idempotent, safe to re-run."""
import sqlite3
import pytest


@pytest.fixture
def raw_db(temp_dir):
    """Bare SQLite connection — no auto_migrate, no SCHEMA."""
    import os
    path = os.path.join(temp_dir, "test.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


class TestAutoMigrate:
    """RED→GREEN: auto_migrate applies pending migrations idempotently."""

    def test_creates_meta_table(self, raw_db):
        """init_meta creates _meta table on first run."""
        from spiderweb.migrate import init_meta

        init_meta(raw_db)

        version = raw_db.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version[0] == "0.2"

    def test_drops_entity_summaries(self, raw_db):
        """Migration drops entity_summaries if it exists."""
        from spiderweb.migrate import init_meta, auto_migrate

        init_meta(raw_db)

        # Manually create the deprecated table
        raw_db.execute("""
            CREATE TABLE entity_summaries (
                entity_name TEXT,
                slug TEXT,
                summary TEXT
            )
        """)
        raw_db.commit()

        # Verify it exists
        row = raw_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_summaries'"
        ).fetchone()
        assert row is not None, "entity_summaries should exist before migration"

        # Run migration — drops it
        applied = auto_migrate(raw_db)
        assert "drop_entity_summaries" in applied, f"Expected drop_entity_summaries in {applied}"

        # Verify it's gone
        row = raw_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_summaries'"
        ).fetchone()
        assert row is None, "entity_summaries should be dropped after migration"

    def test_idempotent(self, raw_db):
        """Running auto_migrate twice produces same result, no errors."""
        from spiderweb.migrate import auto_migrate

        applied_1 = auto_migrate(raw_db)
        applied_2 = auto_migrate(raw_db)

        assert applied_2 == [], f"Second run should apply nothing, got {applied_2}"

    def test_pending_migrations(self, raw_db):
        """pending_migrations shows unapplied migrations."""
        from spiderweb.migrate import init_meta, pending_migrations

        init_meta(raw_db)

        pending = pending_migrations(raw_db)
        assert len(pending) >= 1, f"Expected pending migrations, got {pending}"

        # Apply all
        from spiderweb.migrate import auto_migrate
        auto_migrate(raw_db)

        pending = pending_migrations(raw_db)
        assert len(pending) == 0

    def test_status(self, raw_db):
        """status returns migration overview."""
        from spiderweb.migrate import status, auto_migrate, init_meta

        init_meta(raw_db)

        result = status(raw_db)
        assert result["total"] >= 1
        assert len(result["migrations"]) == result["total"]

        # Before: some not applied
        unapplied = [m for m in result["migrations"] if not m["applied"]]
        assert len(unapplied) >= 1, f"Expected unapplied migrations, got {result}"

        # After: all applied
        auto_migrate(raw_db)
        result = status(raw_db)
        all_applied = all(m["applied"] for m in result["migrations"])
        assert all_applied, f"Not all applied: {result}"
