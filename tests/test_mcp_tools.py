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

    @pytest.mark.anyio
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
    """ReadingService with mocked LLM — think & read."""

    @pytest.mark.anyio
    async def test_think_exact_anchor(self, temp_db, reading_domain):
        """think 精确匹配锚点 → 返回 depths。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('孔子', 'person.philosopher')")
        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('论语', 'work')")
        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('孟子', 'person.philosopher')")
        temp_db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?,?,?,?)",
            ("孔子", "论语", "AUTHORED", 10.0)
        )
        temp_db.execute(
            "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?,?,?,?)",
            ("孔子", "孟子", "INFLUENCED_BY", 5.0)
        )
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.think("孔子")

        assert result["found"] is True
        assert result["anchor"] == "孔子"
        assert "1" in result["depths"]  # 至少 depth 1
        names = [e["name"] for e in result["depths"]["1"]]
        assert "论语" in names

    @pytest.mark.anyio
    async def test_think_alias_anchor(self, temp_db, reading_domain):
        """think 别名匹配锚点 → 找到。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("王守仁", "person.philosopher", '["王阳明","阳明先生"]')
        )
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.think("王阳明")

        assert result["found"] is True
        assert result["anchor"] == "王守仁"

    @pytest.mark.anyio
    async def test_think_not_found(self, temp_db, reading_domain):
        """think 找不到锚点 → 返回候选。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        svc = ReadingService(temp_db, config)
        result = await svc.think("完全不存在的事物")
        assert result["found"] is False
        assert result["candidates"] == []

    @pytest.mark.anyio
    async def test_think_candidates(self, temp_db, reading_domain):
        """think 模糊匹配 → 返回候选列表。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
            ("王阳明", "person.philosopher")
        )
        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type) VALUES (?, ?)",
            ("王守仁", "person.philosopher")
        )
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.think("王")
        assert result["found"] is False
        assert "王阳明" in result["candidates"] or "王守仁" in result["candidates"]

    @pytest.mark.anyio
    async def test_think_hot_stops(self, temp_db, reading_domain):
        """think 遇到热点节点 → 提前停止。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        # Set up: 锚点 A → B (hot)
        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('A', 'concept')")
        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('B', 'concept')")
        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('C', 'concept')")
        temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES ('D', 'concept')")
        temp_db.execute("INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES ('A','B','MENTIONS', 5.0)")
        temp_db.execute("INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES ('A','C','MENTIONS', 3.0)")
        temp_db.execute("INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES ('A','D','MENTIONS', 1.0)")
        # B has high footprint → hot
        temp_db.execute(
            "INSERT INTO entity_traces (entity_name, source, count) VALUES ('B', 'navigate', 6)"
        )
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.think("A")
        assert result["found"] is True
        # Only depth 1 because B is hot → should stop
        assert "1" in result["depths"]
        assert "2" not in result["depths"]  # shouldn't expand further

    @pytest.mark.anyio
    async def test_think_depth_expansion(self, temp_db, reading_domain):
        """think 多层展开，每层 top 3。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        # A → B1, B2, B3 → C1, C2, C3
        for name in ["A", "B1", "B2", "B3", "C1", "C2", "C3"]:
            temp_db.execute("INSERT INTO entities (canonical_name, entity_type) VALUES (?, 'concept')", (name,))
        for b in ["B1", "B2", "B3"]:
            temp_db.execute("INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES ('A', ?, 'MENTIONS', 5.0)", (b,))
        for c in ["C1", "C2", "C3"]:
            temp_db.execute("INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES ('B1', ?, 'MENTIONS', 5.0)", (c,))
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.think("A")
        assert result["found"] is True
        assert "1" in result["depths"]
        assert len(result["depths"]["1"]) <= 3  # top 3
        names_d1 = {e["name"] for e in result["depths"]["1"]}
        assert names_d1 == {"B1", "B2", "B3"}

    @pytest.mark.anyio
    async def test_read_returns_body(self, temp_db, reading_domain):
        """read 返回完整段落 + 出处。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "jieba"}

        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/t.md', '测试书', '作者')"
        )
        doc_id = 1
        temp_db.execute(
            "INSERT INTO chunks (doc_id, section_path, body) VALUES (?, '第一章', ?)",
            (doc_id, "孔子曰：学而时习之，不亦说乎？")
        )
        temp_db.execute(
            "INSERT INTO chunks_fts (rowid, body) VALUES (?, ?)",
            (1, "孔子 曰 学而时习之 不亦说乎")
        )
        temp_db.execute("INSERT INTO _meta (key, value) VALUES ('has_vectors', '0')")
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.read("孔子")

        assert "results" in result
        assert len(result["results"]) > 0
        assert "body" in result["results"][0]
        assert "book" in result["results"][0]
        assert "学而时习之" in result["results"][0]["body"]

    @pytest.mark.anyio
    async def test_read_source_filter(self, temp_db, reading_domain):
        """read 限定 source → 只返回该书的段落。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "jieba"}

        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/t.md', '测试书', '作者')"
        )
        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/t2.md', '另一本书', '作者')"
        )
        temp_db.execute(
            "INSERT INTO chunks (doc_id, section_path, body) VALUES (1, '第一章', ?)",
            ("孔子曰：学而时习之。",)
        )
        temp_db.execute(
            "INSERT INTO chunks (doc_id, section_path, body) VALUES (2, '第一章', ?)",
            ("孔子在另一本书里的内容。",)
        )
        temp_db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (1, '孔子 曰 学而时习之')")
        temp_db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (2, '孔子 在 另 一 本 书 里 的 内容')")
        temp_db.execute("INSERT INTO _meta (key, value) VALUES ('has_vectors', '0')")
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.read("孔子", source="测试书", top_n=5)

        assert len(result["results"]) > 0
        for r in result["results"]:
            assert r["book"] == "测试书"

    @pytest.mark.anyio
    async def test_read_source_not_found(self, temp_db, reading_domain):
        """read 在指定书里找不到 → 返回空+提示。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "jieba"}

        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/t.md', '测试书', '作者')"
        )
        temp_db.execute(
            "INSERT INTO chunks (doc_id, section_path, body) VALUES (1, '第一章', ?)",
            ("完全无关的内容。",)
        )
        temp_db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (1, '完全无关的内容')")
        temp_db.execute("INSERT INTO _meta (key, value) VALUES ('has_vectors', '0')")
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.read("孔子", source="测试书")

        assert result["results"] == []
        assert "没有找到" in result.get("message", "")

    @pytest.mark.anyio
    async def test_read_default_top_n(self, temp_db, reading_domain):
        """read 不传 top_n 默认 = 3。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "none"}

        svc = ReadingService(temp_db, config)
        # Just call with default — no exception is the main check
        result = await svc.read("不存在")
        assert result["results"] == []

    @pytest.mark.anyio
    async def test_read_without_source_returns_all(self, temp_db, reading_domain):
        """read 不限定来源 → 返回所有书的匹配。"""
        from spiderweb.engine.domain import load_domain
        from spiderweb.service.reading_service import ReadingService

        config = load_domain(reading_domain["domain_dir"])
        config.data_dir = reading_domain["data_dir"]
        config.llm = {"driver": "openai", "model": "deepseek-chat", "base_url": "http://localhost:0"}
        config.embedding = {"driver": "none"}
        config.tokenizer = {"driver": "jieba"}

        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/a.md', '书A', '作者')"
        )
        temp_db.execute(
            "INSERT INTO docs (path, title, author) VALUES ('/tmp/b.md', '书B', '作者')"
        )
        temp_db.execute(
            "INSERT INTO chunks (doc_id, section_path, body) VALUES (1, '章', ?)",
            ("hello world",)
        )
        temp_db.execute(
            "INSERT INTO chunks (doc_id, section_path, body) VALUES (2, '章', ?)",
            ("hello again",)
        )
        temp_db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (1, 'hello world')")
        temp_db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (2, 'hello again')")
        temp_db.execute("INSERT INTO _meta (key, value) VALUES ('has_vectors', '0')")
        temp_db.commit()

        svc = ReadingService(temp_db, config)
        result = await svc.read("hello")

        assert len(result["results"]) > 0
        books = {r["book"] for r in result["results"]}
        assert "书A" in books and "书B" in books

    # ── _find_anchor tests (replacing old _find_seed_entity tests) ──

    def test_find_anchor_exact(self, temp_db):
        """_find_anchor 精确匹配返回实体。"""
        from spiderweb.service.reading_service import ReadingService
        from spiderweb.engine.domain import DomainConfig

        svc = ReadingService(temp_db, DomainConfig())
        # No entities → None
        assert svc._find_anchor("王阳明") is None

        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("王守仁", "person.philosopher", '["王阳明"]')
        )
        temp_db.commit()

        assert svc._find_anchor("王阳明")["name"] == "王守仁"

    def test_find_anchor_cjk(self, temp_db):
        """_find_anchor CJK 递进前缀匹配。"""
        from spiderweb.service.reading_service import ReadingService
        from spiderweb.engine.domain import DomainConfig

        svc = ReadingService(temp_db, DomainConfig())
        temp_db.execute(
            "INSERT INTO entities (canonical_name, entity_type, aliases_json) VALUES (?, ?, ?)",
            ("王守仁", "person.philosopher", '["王阳明","阳明先生"]')
        )
        temp_db.commit()

        # "阳明知行合一" → progressive prefix finds "王守仁" via aliases
        result = svc._find_anchor("阳明知行合一")
        assert result is not None
        assert result["name"] == "王守仁"
