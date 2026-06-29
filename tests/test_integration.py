"""Integration smoke test — real LLM, end-to-end.

SKIP if SPIDERWEB_LLM_API_KEY is not set.
"""
import json
import os
import pytest


HAS_LLM_KEY = bool(os.environ.get("SPIDERWEB_LLM_API_KEY"))

pytestmark = pytest.mark.skipif(
    not HAS_LLM_KEY,
    reason="SPIDERWEB_LLM_API_KEY not set — set it to run integration test"
)


@pytest.mark.asyncio
@pytest.mark.integration
class TestFullPipeline:
    """End-to-end: ingest → build graph → record insight → search."""

    async def test_full_pipeline(self, temp_db):
        """Ingest a doc, extract entities with real LLM, record insight, verify data."""
        import subprocess, tempfile
        from spiderweb.engine.domain import load_domain

        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")

        # 1. Create test document
        doc_text = """# 孙子兵法

## 始计篇

孙子曰：兵者，国之大事，死生之地，存亡之道，不可不察也。

故经之以五事，校之以计，而索其情：一曰道，二曰天，三曰地，四曰将，五曰法。
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(doc_text)
            doc_path = f.name

        try:
            # 2. Ingest
            from spiderweb.engine.l1.ingest import ingest_file
            title, author, chunks = ingest_file(doc_path)

            cursor = temp_db.execute(
                "INSERT INTO docs (path, title, author) VALUES (?, ?, ?)",
                (doc_path, title, author)
            )
            doc_id = cursor.lastrowid

            chunk_ids = []
            for ch in chunks:
                cid = temp_db.execute(
                    "INSERT INTO chunks (doc_id, section_path, heading_level, body, line_start) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, ch.section_path, ch.heading_level, ch.body, ch.line_start)
                ).lastrowid
                temp_db.execute("INSERT INTO chunks_fts(rowid, body) VALUES (?, ?)", (cid, ch.body))
                chunk_ids.append(cid)
            temp_db.commit()

            assert len(chunks) >= 1
            assert doc_id > 0

            # 3. Build graph with REAL LLM
            from spiderweb.engine.l2.build import build_graph
            result = await build_graph(temp_db, config, doc_ids=[doc_id])

            assert result["chunks_processed"] >= 1
            assert result["entities_found"] > 0, (
                f"Real LLM should extract at least one entity. Got: {result}"
            )
            assert result["entities_registered"] > 0

            # 4. Verify entities in DB
            entities = temp_db.execute(
                "SELECT canonical_name, entity_type FROM entities WHERE source = 'auto'"
            ).fetchall()
            assert len(entities) > 0, "Entities with source='auto' should exist in DB"

            # 5. Search entities
            entity_count = temp_db.execute(
                "SELECT COUNT(*) FROM entities"
            ).fetchone()[0]
            assert entity_count >= 1

            # 6. Record insight with REAL LLM
            from spiderweb.mcp.server import _insight_record, _insight_list
            await _insight_record(temp_db, config, {
                "title": "读孙子兵法有感",
                "content": "今天读了孙子兵法的始计篇，孙子强调战争是国家大事，必须精心谋划。"
            })

            # 7. Verify insight was recorded
            insights = await _insight_list(temp_db, {"limit": 10})
            insight_data = json.loads(insights[0].text)
            assert len(insight_data["insights"]) >= 1

            # 8. Check insights reference entities
            rows = temp_db.execute(
                "SELECT entities_json FROM insights"
            ).fetchall()
            for row in rows:
                if row[0]:
                    entity_names = json.loads(row[0])
                    assert len(entity_names) > 0, f"L3 insight should link to entities: {row}"

        finally:
            os.unlink(doc_path)
