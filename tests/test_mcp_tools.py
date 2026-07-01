"""Test engine search functions + ReadingService (replaces old MCP handler tests)."""
import json
import pytest


class TestGraphStats:
    """graph_stats returns doc/entity/relation/insight counts."""

    def test_empty_db(self, temp_db):
        from spiderweb.engine.l2.search import graph_stats
        stats = graph_stats(temp_db)
        assert stats["docs"] == 0
        assert stats["entities"] == 0
        assert stats["relations"] == 0
        assert stats["insights"] == 0

    def test_with_data(self, temp_db):
        from spiderweb.engine.l2.search import graph_stats

        temp_db.execute("INSERT INTO docs (path, title) VALUES ('/tmp/a.md', 'A')")
        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('X')")
        temp_db.execute("INSERT INTO entities (canonical_name) VALUES ('Y')")
        temp_db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type) VALUES ('X','Y','MENTIONS')"
        )
        temp_db.commit()

        stats = graph_stats(temp_db)
        assert stats["docs"] == 1
        assert stats["entities"] == 2
        assert stats["relations"] == 1


class TestSearchEntities:
    """search_entities finds by name or alias."""

    def test_finds_by_canonical_name(self, temp_db):
        from spiderweb.engine.l2.search import search_entities

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("康德", "person.philosopher", '["Immanuel Kant"]')
        )
        temp_db.commit()

        results = search_entities(temp_db, "康德")
        assert len(results) == 1
        assert results[0]["name"] == "康德"
        assert results[0]["type"] == "person.philosopher"

    def test_finds_by_alias(self, temp_db):
        from spiderweb.engine.l2.search import search_entities

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("Sunzi", "person", '["孙子","孙武"]')
        )
        temp_db.commit()

        results = search_entities(temp_db, "孙武")
        assert len(results) == 1

    def test_empty_result(self, temp_db):
        from spiderweb.engine.l2.search import search_entities

        results = search_entities(temp_db, "不存在")
        assert len(results) == 0


class TestGraphNavigate:
    """graph_navigate returns edges from a seed entity."""

    def test_returns_edges(self, temp_db):
        from spiderweb.engine.l2.search import graph_navigate

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

        result = graph_navigate(temp_db, "孔子")
        assert result["position"]["entity"] == "孔子"
        neighbor_names = [e["neighbor"] for e in result["anchored"] + result["exploration"]]
        assert "论语" in neighbor_names


class TestInsightWrite:
    """write_insight with mocked LLM."""

    @pytest.mark.asyncio
    async def test_write_insight(self, temp_db, reading_domain):
        from spiderweb.engine.domain import load_domain
        from spiderweb.engine.l3.insight import write_insight

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        result = await write_insight(temp_db, config, "测试笔记", "这是一条测试内容。")
        assert result["ok"] is True
        assert result["slug"] == "测试笔记"


class TestReadingService:
    """ReadingService with mocked LLM."""

    @pytest.mark.asyncio
    async def test_discover_status(self, temp_db, reading_domain):
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        svc = ReadingService(temp_db, config)
        # Force status intent via content match
        result = await svc.discover("概况")
        assert result["intent"] == "status"
        assert "stats" in result

    @pytest.mark.asyncio
    async def test_discover_lookup_regex(self, temp_db, reading_domain):
        """Regex fallback should classify '王阳明' as lookup when LLM fails."""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        svc = ReadingService(temp_db, config)
        intent = svc._regex_intent("王阳明是谁")
        assert intent == "lookup"

    def test_regex_intent(self, temp_db, reading_domain):
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        svc = ReadingService(temp_db, config)

        assert svc._regex_intent("我之前记过什么") == "recall"
        assert svc._regex_intent("孔子和孟子的区别") == "compare"
        assert svc._regex_intent("探索一下关系网") == "explore"
        assert svc._regex_intent("统计数据") == "status"
        assert svc._regex_intent("随便问问") == "lookup"

    @pytest.mark.asyncio
    async def test_discover_with_data(self, temp_db, reading_domain):
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        # Add test data
        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/t.md', '测试书', '作者')"
        )
        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type) VALUES ('孔子', 'person.philosopher')"
        )
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.discover("孔子")

        assert result["query"] == "孔子"
        assert result["intent"] in ("lookup", "status")
