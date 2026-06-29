"""Test L3 insight reverse extraction (mock LLM)."""
import pytest
from spiderweb.engine.domain import load_domain
from conftest import MockLLM


def _get_reading_config():
    return load_domain("/home/ubuntu/projects/spiderweb/domains/reading")


class TestReverseExtract:
    """RED→GREEN: reverse_extract finds entities in user notes."""

    @pytest.mark.asyncio
    async def test_extracts_entities_from_note(self):
        """Mock LLM returns entities mentioned in the insight text."""
        from spiderweb.engine.l3.insight import reverse_extract

        config = _get_reading_config()
        llm = MockLLM({
            "entities": [
                {"name": "论语", "type": "work.classic"},
                {"name": "孔子", "type": "person.philosopher"},
            ],
            "relations": [
                {"entity_a": "论语", "entity_b": "孔子", "relation_type": "MENTIONS"},
            ]
        })

        entities, relations = await reverse_extract(
            llm, config, "今天读了论语，孔子的思想很有意思。"
        )

        assert len(entities) == 2
        assert entities[0]["name"] == "论语"
        assert entities[1]["name"] == "孔子"
        assert len(relations) >= 1

    @pytest.mark.asyncio
    async def test_handles_empty_note(self):
        """Empty/extremely short note → gracefully returns empty lists."""
        from spiderweb.engine.l3.insight import reverse_extract

        config = _get_reading_config()
        llm = MockLLM({"entities": [], "relations": []})

        entities, relations = await reverse_extract(llm, config, "嗯。")
        assert entities == []
        assert relations == []

    @pytest.mark.asyncio
    async def test_handles_non_json(self):
        """Non-JSON response → graceful empty fallback."""
        from spiderweb.engine.l3.insight import reverse_extract

        config = _get_reading_config()
        llm = MockLLM("invalid response")

        entities, relations = await reverse_extract(llm, config, "test")
        assert entities == []


class TestLinkToEntities:
    """RED→GREEN: link_insight_to_entities writes to DB correctly."""

    def test_registers_new_entities(self, temp_db):
        """New entities from insight are created in entities table."""
        from spiderweb.engine.l3.insight import link_insight_to_entities

        config = _get_reading_config()

        # Create an insight row first
        temp_db.execute(
            "INSERT INTO insights (slug, title, content) VALUES (?, ?, ?)",
            ("test-insight", "测试笔记", "关于老子的思考")
        )
        insight_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]

        entities = [{"name": "老子", "type": "person.philosopher"}]
        relations = []

        result = link_insight_to_entities(temp_db, config, insight_id, entities, relations)

        assert result["entities_extracted"] == 1
        assert result["entities_registered"] == 1

        # Verify entity in DB
        row = temp_db.execute(
            "SELECT canonical_name, entity_type, source FROM entities WHERE canonical_name = ?",
            ("老子",)
        ).fetchone()
        assert row is not None
        assert row[0] == "老子"
        assert row[2] == "l3_insight"

    def test_creates_mentions_edges(self, temp_db):
        """MENTIONS edges from insight → entities and entity↔entity."""
        from spiderweb.engine.l3.insight import link_insight_to_entities

        config = _get_reading_config()

        temp_db.execute(
            "INSERT INTO insights (slug, title, content) VALUES (?, ?, ?)",
            ("multi-entity", "多实体笔记", "孔子和老子的对比")
        )
        insight_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]

        entities = [
            {"name": "孔子", "type": "person.philosopher"},
            {"name": "老子", "type": "person.philosopher"},
        ]
        relations = [
            {"entity_a": "孔子", "entity_b": "老子", "relation_type": "MENTIONS"},
        ]

        result = link_insight_to_entities(temp_db, config, insight_id, entities, relations)

        assert result["entities_extracted"] == 2

        # Verify MENTIONS edges exist
        count = temp_db.execute(
            "SELECT COUNT(*) FROM relations WHERE relation_type = 'MENTIONS'"
        ).fetchone()[0]
        assert count >= 2  # insight→entity edges + co-occurrence
