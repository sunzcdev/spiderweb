"""Test MCP tool handlers — verify routing, return shapes, and error handling."""
import json
import pytest


class TestGraphStats:
    """RED→GREEN: graph_stats returns doc/entity/relation/insight counts."""

    @pytest.mark.asyncio
    async def test_empty_db(self, temp_db):
        from spiderweb.mcp.server import _graph_stats

        result = await _graph_stats(temp_db)
        data = json.loads(result[0].text)
        assert data["docs"] == 0
        assert data["entities"] == 0
        assert data["relations"] == 0
        assert data["insights"] == 0

    @pytest.mark.asyncio
    async def test_with_data(self, temp_db):
        from spiderweb.mcp.server import _graph_stats

        temp_db.execute("INSERT INTO docs (path, title) VALUES ('/tmp/a.md', 'A')")
        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('X')")
        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('Y')")
        temp_db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type) VALUES ('X','Y','MENTIONS')"
        )
        temp_db.commit()

        result = await _graph_stats(temp_db)
        data = json.loads(result[0].text)
        assert data["docs"] == 1
        assert data["entities"] == 2
        assert data["relations"] == 1


class TestSearchEntities:
    """RED→GREEN: search_entities finds by name or alias."""

    @pytest.mark.asyncio
    async def test_finds_by_canonical_name(self, temp_db):
        from spiderweb.mcp.server import _search_entities

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("康德", "person.philosopher", '["Immanuel Kant"]')
        )
        temp_db.commit()

        result = await _search_entities(temp_db, {"query": "康德"})
        data = json.loads(result[0].text)
        assert data["count"] == 1
        assert data["results"][0]["name"] == "康德"
        assert data["results"][0]["type"] == "person.philosopher"

    @pytest.mark.asyncio
    async def test_finds_by_alias(self, temp_db):
        from spiderweb.mcp.server import _search_entities

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("Sunzi", "person", '["孙子","孙武"]')
        )
        temp_db.commit()

        result = await _search_entities(temp_db, {"query": "孙武"})
        data = json.loads(result[0].text)
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_empty_result(self, temp_db):
        from spiderweb.mcp.server import _search_entities

        result = await _search_entities(temp_db, {"query": "不存在"})
        data = json.loads(result[0].text)
        assert data["count"] == 0


class TestEntityRegister:
    """RED→GREEN: entity_register creates or updates entities."""

    @pytest.mark.asyncio
    async def test_creates_new_entity(self, temp_db):
        from spiderweb.mcp.server import _entity_register

        result = await _entity_register(temp_db, {
            "name": "墨子", "entity_type": "person.philosopher",
            "aliases": ["墨翟"], "description": "墨家创始人"
        })
        data = json.loads(result[0].text)
        assert data["ok"] is True
        assert data["name"] == "墨子"

        row = temp_db.execute(
            "SELECT entity_type, source FROM entities WHERE canonical_name = ?",
            ("墨子",)
        ).fetchone()
        assert row[0] == "person.philosopher"
        assert row[1] == "manual"


class TestRelationCRUD:
    """RED→GREEN: relation_set creates, relation_list returns."""

    @pytest.mark.asyncio
    async def test_set_and_list(self, temp_db):
        from spiderweb.mcp.server import _relation_set, _relation_list

        await _relation_set(temp_db, {
            "entity_a": "孔子", "entity_b": "论语",
            "relation_type": "AUTHORED", "weight": 1.0
        })

        result = await _relation_list(temp_db, {"entity_name": "孔子"})
        data = json.loads(result[0].text)
        assert data["entity"] == "孔子"
        assert len(data["relations"]) == 1
        assert data["relations"][0]["a"] == "孔子"


class TestGraphNavigate:
    """RED→GREEN: graph_navigate returns edges from a seed entity."""

    @pytest.mark.asyncio
    async def test_returns_edges(self, temp_db):
        from spiderweb.mcp.server import _graph_navigate
        from spiderweb.engine.domain import load_domain

        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")

        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('孔子')")
        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('论语')")
        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('孟子')")
        temp_db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?,?,?,?)",
            ("孔子", "论语", "AUTHORED", 10.0)
        )
        temp_db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?,?,?,?)",
            ("孔子", "孟子", "INFLUENCED_BY", 5.0)
        )
        temp_db.commit()

        result = await _graph_navigate(temp_db, config, {"seed": "孔子"})
        data = json.loads(result[0].text)
        assert data["position"]["entity"] == "孔子"
        neighbor_names = [e["neighbor"] for e in data["anchored"] + data["exploration"]]
        assert "论语" in neighbor_names


class TestDocGet:
    """RED→GREEN: doc_get returns chunk by ID."""

    @pytest.mark.asyncio
    async def test_returns_chunk(self, temp_db):
        from spiderweb.mcp.server import _doc_get

        temp_db.execute("INSERT INTO docs (path, title) VALUES ('/tmp/x.md', '测试')")
        doc_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        temp_db.execute(
            "INSERT INTO chunks (doc_id, body, section_path) VALUES (?,?,?)",
            (doc_id, "正文内容", "第一章")
        )
        chunk_id = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        temp_db.commit()

        result = await _doc_get(temp_db, {"chunk_id": chunk_id})
        data = json.loads(result[0].text)
        assert data["body"] == "正文内容"
        assert data["section"] == "第一章"
        assert data["doc_title"] == "测试"


class TestInsightCRUD:
    """RED→GREEN: insight_record creates, insight_list returns."""

    @pytest.mark.asyncio
    async def test_record_and_list(self, temp_db):
        from spiderweb.mcp.server import _insight_record, _insight_list
        from spiderweb.engine.domain import load_domain
        from unittest.mock import patch, AsyncMock

        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")

        # Mock LLM to avoid real API call
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = '{"entities": [], "relations": []}'

        with patch('spiderweb.engine.providers.create_llm_provider', return_value=mock_llm):
            await _insight_record(temp_db, config, {
                "title": "我的笔记", "content": "这是一条测试笔记。"
            })

        result = await _insight_list(temp_db, {"limit": 10})
        data = json.loads(result[0].text)
        assert len(data["insights"]) == 1
        assert data["insights"][0]["title"] == "我的笔记"
