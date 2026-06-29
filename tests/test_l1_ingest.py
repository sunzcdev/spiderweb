"""Test L1 document ingestion and chunk search."""
import os
import pytest


class TestIngestFile:
    """RED→GREEN: ingest_file parses markdown correctly."""

    def test_parse_markdown(self, temp_dir):
        """A .md file produces correct title and chunks."""
        from spiderweb.engine.l1.ingest import ingest_file

        path = os.path.join(temp_dir, "test.md")
        with open(path, "w") as f:
            f.write("# 论语\n\n## 学而篇\n\n子曰：学而时习之，不亦说乎。\n\n## 为政篇\n\n子曰：为政以德，譬如北辰。\n")

        title, author, chunks = ingest_file(path)

        assert title == "论语"
        assert author == ""
        assert len(chunks) == 3
        # Chunk 0: before first ## (the "# 论语" heading treated as bodyless intro)
        # Chunk 1: 学而篇
        assert chunks[1].section_path == "学而篇"
        assert "学而时习之" in chunks[1].body
        # Chunk 2: 为政篇
        assert chunks[2].section_path == "为政篇"

    def test_plain_text(self, temp_dir):
        """.txt file gets title from filename, no author."""
        from spiderweb.engine.l1.ingest import ingest_file

        path = os.path.join(temp_dir, "notes.txt")
        with open(path, "w") as f:
            f.write("Some plain text without headers.")

        title, author, chunks = ingest_file(path)
        assert title == "notes"
        assert author == ""

    def test_rejects_unsupported(self, temp_dir):
        """Unsupported file types raise ValueError."""
        from spiderweb.engine.l1.ingest import ingest_file

        path = os.path.join(temp_dir, "data.pdf")
        with open(path, "w") as f:
            f.write("fake pdf")

        with pytest.raises(ValueError, match="Unsupported"):
            ingest_file(path)


class TestDocIngestDB:
    """RED→GREEN: ingested doc inserts into DB and is FTS5-searchable."""

    def test_inserts_doc_and_chunks(self, temp_db, temp_dir):
        """Ingest a doc → doc row exists, chunks exist, FTS5 search finds it."""
        from spiderweb.engine.l1.ingest import ingest_file, Chunk

        path = os.path.join(temp_dir, "test.md")
        with open(path, "w") as f:
            f.write("# 道德经\n\n## 道可道\n\n道可道，非常道。名可名，非常名。\n")

        title, author, chunks = ingest_file(path)

        # Write to DB (as _doc_ingest does)
        cur = temp_db.execute("INSERT INTO docs (path, title, author) VALUES (?, ?, ?)",
                              (path, title, author))
        doc_id = cur.lastrowid

        chunk_ids = []
        for ch in chunks:
            cid = temp_db.execute(
                "INSERT INTO chunks (doc_id, section_path, heading_level, body, line_start) VALUES (?, ?, ?, ?, ?)",
                (doc_id, ch.section_path, ch.heading_level, ch.body, ch.line_start)
            ).lastrowid
            temp_db.execute("INSERT INTO chunks_fts(rowid, body) VALUES (?, ?)", (cid, ch.body))
            chunk_ids.append(cid)

        temp_db.commit()

        # Verify doc
        row = temp_db.execute("SELECT title FROM docs WHERE id = ?", (doc_id,)).fetchone()
        assert row[0] == "道德经"

        # Verify chunks
        count = temp_db.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()
        assert count[0] >= 1

        # Verify FTS5 search
        results = temp_db.execute(
            "SELECT body FROM chunks_fts WHERE chunks_fts MATCH ?", ("道可道",)
        ).fetchall()
        assert len(results) >= 1
        assert "道可道" in results[0][0]
