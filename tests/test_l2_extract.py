"""Test L2 graph building via LLM extraction (mock LLM)."""
import pytest
from spiderweb.engine.domain import load_domain
from conftest import MockLLM, FailingLLM


def _get_reading_config():
    return load_domain("/home/ubuntu/projects/spiderweb/domains/reading")


class TestExtractFromChunks:
    """RED→GREEN: extract_from_chunks returns entities and relations."""

    @pytest.mark.asyncio
    async def test_extracts_entities_and_relations(self):
        """With valid LLM response, extraction returns correct structures."""
        from spiderweb.engine.l2.extract import extract_from_chunks

        config = _get_reading_config()
        llm = MockLLM({
            "entities": [
                {"name": "孔子", "type": "person.philosopher", "aliases": ["孔丘"],
                 "description": "儒家创始人"},
                {"name": "论语", "type": "work.classic", "aliases": [],
                 "description": "孔子门人所编纂的对话集"},
            ],
            "relations": [
                {"entity_a": "孔子", "entity_b": "论语", "relation_type": "AUTHORED"},
            ]
        })

        chunks = [(1, "孔子是中国古代伟大的思想家。"), (2, "论语记载了孔子的言行。")]
        entities, relations = await extract_from_chunks(llm, config, chunks, batch_size=2)

        assert len(entities) == 2
        assert entities[0]["name"] == "孔子"
        assert entities[0]["type"] == "person.philosopher"
        assert len(relations) == 1
        assert relations[0]["entity_a"] == "孔子"
        assert relations[0]["relation_type"] == "AUTHORED"

    @pytest.mark.asyncio
    async def test_handles_empty_response(self):
        """Empty LLM response returns empty lists without crashing."""
        from spiderweb.engine.l2.extract import extract_from_chunks

        config = _get_reading_config()
        llm = MockLLM({"entities": [], "relations": []})
        chunks = [(1, "一些文字。")]

        entities, relations = await extract_from_chunks(llm, config, chunks)
        assert entities == []
        assert relations == []

    @pytest.mark.asyncio
    async def test_handles_non_json_response(self):
        """Non-JSON LLM response returns empty lists (graceful degradation)."""
        from spiderweb.engine.l2.extract import extract_from_chunks

        config = _get_reading_config()
        llm = MockLLM("这不是 JSON，只是一些随机文字输出。")
        chunks = [(1, "测试文本。")]

        entities, relations = await extract_from_chunks(llm, config, chunks)
        assert entities == []
        assert relations == []

    @pytest.mark.asyncio
    async def test_falls_back_to_hardcoded_prompts(self):
        """When domain has no prompt files, hardcoded prompts are used."""
        from spiderweb.engine.l2.extract import extract_from_chunks
        from spiderweb.engine.domain import DomainConfig

        config = DomainConfig()  # No prompts_dir — triggers fallback
        config.entity_types = {}
        config.relation_types = {}
        config.entity_description_template = ""

        llm = MockLLM({"entities": [], "relations": []})
        chunks = [(1, "test")]
        entities, relations = await extract_from_chunks(llm, config, chunks)
        assert entities == []
        assert relations == []


class TestGraphBuildWritesDB:
    """RED→GREEN: build_graph writes entities and relations to DB."""

    @pytest.mark.asyncio
    async def test_writes_entities_to_db(self, temp_db):
        """build_graph extracts entities and writes them to the DB."""
        import sys, json
        from unittest.mock import patch, AsyncMock

        config = _get_reading_config()
        mock_llm = MockLLM({
            "entities": [
                {"name": "老子", "type": "person.philosopher", "aliases": ["李耳"],
                 "description": "道家创始人"},
            ],
            "relations": [],
        })

        # Ingest a doc first
        doc_id = temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES (?, ?, ?)",
            ("/tmp/test.md", "测试", "佚名")
        ).lastrowid
        temp_db.execute(
            "INSERT INTO chunks (doc_id, body) VALUES (?, ?)",
            (doc_id, "老子是道家创始人。")
        )
        temp_db.commit()

        # Patch create_llm_provider to return our mock
        with patch('spiderweb.engine.providers.create_llm_provider', return_value=mock_llm):
            from spiderweb.engine.l2.build import build_graph
            result = await build_graph(temp_db, config, doc_ids=[doc_id])

        assert result["entities_found"] == 1
        assert result["entities_registered"] == 1

        # Verify DB
        row = temp_db.execute(
            "SELECT canonical_name, entity_type, description FROM entities WHERE canonical_name = ?",
            ("老子",)
        ).fetchone()
        assert row is not None
        assert row[0] == "老子"
        assert row[1] == "person.philosopher"
