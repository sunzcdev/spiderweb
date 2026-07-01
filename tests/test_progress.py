"""Test ingest process via ReadingService (replaces old _doc_ingest progress test)."""
import json
import tempfile
import os
import pytest


@pytest.mark.asyncio
async def test_ingest_markdown(temp_db, reading_domain):
    """ReadingService.ingest ingests a .md file with all steps completed."""
    from spiderweb.engine.domain import load_domain
    from spiderweb.service.reading_service import ReadingService

    config = load_domain(reading_domain["domain_dir"])
    config.data_dir = reading_domain["data_dir"]
    config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
    config.embedding = {"driver": "none"}
    config.tokenizer = {"driver": "none"}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# 测试书\n\n孔子曰：学而时习之，不亦说乎。\n\n孟子曰：尽信书则不如无书。\n")
        test_path = f.name

    try:
        svc = ReadingService(temp_db, config)
        result = await svc.ingest(test_path)

        assert result["ok"] is True
        assert result["chunks"] > 0
        assert result["doc_id"] > 0
        assert result["title"] == "测试书"
        assert isinstance(result["vectors"], int)

        # Verify data written to DB
        doc = temp_db.execute(
            "SELECT title, author FROM docs WHERE id = ?", (result["doc_id"],)
        ).fetchone()
        assert doc is not None
        assert doc[0] == "测试书"

        chunks = temp_db.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (result["doc_id"],)
        ).fetchone()
        assert chunks[0] == result["chunks"]

    finally:
        os.unlink(test_path)
