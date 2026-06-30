"""Test progress notifications via direct engine call (bypass MCP stdio)."""
import json
import tempfile
import os
import pytest


@pytest.mark.asyncio
@pytest.mark.integration
async def test_doc_ingest_progress_callback():
    """doc_ingest calls progress callback with ratio/message, returns complete result."""
    import os
    os.environ["SPIDERWEB_DOMAIN"] = "/home/ubuntu/projects/spiderweb/domains/reading"
    os.environ["SPIDERWEB_LLM_API_KEY"] = "sk-02deafc15a634f0ab5f63aeec4f8f86f"
    os.environ["SPIDERWEB_EMBEDDING_API_KEY"] = ""  # skip slow vector

    from spiderweb.engine.domain import load_domain
    from spiderweb.engine.db import get_db, get_db_path
    from spiderweb.mcp.server import _doc_ingest, _doc_delete
    from spiderweb.engine.db import get_db_path

    config = load_domain(os.environ["SPIDERWEB_DOMAIN"])
    db = get_db(get_db_path(config.data_dir))

    # Create tiny test doc
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# 测试书\n\n孔子曰：学而时习之，不亦说乎。\n\n孟子曰：尽信书则不如无书。\n")
        test_path = f.name

    try:
        progress_msgs = []

        async def progress(ratio, msg):
            progress_msgs.append((ratio, msg))

        result = await _doc_ingest(db, config, {
            "file_path": test_path,
            "title": "TDD测试书",
        }, progress=progress)

        data = json.loads(result[0].text)

        # Assert: complete result
        assert data["ok"] is True
        assert isinstance(data["vectors"], int), f"vectors={data['vectors']}"
        assert "graph" in data
        assert data["chunks"] > 0

        # Assert: progress fired
        print(f"Progress msgs: {len(progress_msgs)}")
        for r, m in progress_msgs:
            print(f"  {r:.0%} — {m}")
        assert len(progress_msgs) >= 2, f"Expected >=2 progress msgs, got {len(progress_msgs)}"

        # Cleanup
        await _doc_delete(db, {"doc_id": data["doc_id"]})

    finally:
        os.unlink(test_path)
