"""Shared test fixtures for spiderweb."""
import json
import os
import pytest
import tempfile

# Reset module-level DB singleton before any test imports it
import spiderweb.engine.db as db_module

@pytest.fixture(autouse=True)
def _reset_db_singleton():
    """Ensure a fresh DB connection for each test."""
    db_module._conn = None
    yield
    db_module._conn = None


@pytest.fixture
def temp_dir():
    """Temporary directory that cleans up after the test."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def temp_db(temp_dir):
    """Initialize a fresh spiderweb DB in a temp directory."""
    db_module._conn = None
    db_path = db_module.get_db_path(temp_dir)
    db = db_module.get_db(db_path)
    yield db
    try:
        db.close()
    except Exception:
        pass
    db_module._conn = None


@pytest.fixture
def reading_domain(temp_dir):
    """Create a minimal reading domain pack in a temp dir and load it."""
    import yaml
    from spiderweb.engine.domain import DomainConfig
    from pathlib import Path

    domain_dir = Path(temp_dir) / "domains" / "reading"
    domain_dir.mkdir(parents=True)

    config = {
        "name": "reading",
        "version": "1.0",
        "entity_types": {
            "person": {"label": "人物", "subtypes": {
                "philosopher": {"label": "哲学家"}
            }},
            "concept": {"label": "概念", "subtypes": {
                "doctrine": {"label": "学说"}
            }},
        },
        "relation_types": {
            "CREATED": {"label": "创作了", "entity_a": "person", "entity_b": "work"},
            "DISCUSSES": {"label": "论述了", "entity_a": "work", "entity_b": "concept"},
            "MENTIONS": {"label": "提及", "entity_a": "*", "entity_b": "*"},
        },
        "entity_description_template": "",
    }
    with open(domain_dir / "domain.yaml", "w") as f:
        yaml.dump(config, f, allow_unicode=True)

    # Also need data dir
    data_dir = Path(temp_dir) / "data"
    data_dir.mkdir()

    return {
        "domain_dir": str(domain_dir),
        "data_dir": str(data_dir),
        "config": config,
    }


class MockLLM:
    """Mock LLM provider that returns controlled JSON."""

    def __init__(self, response: dict | list | str = None):
        self.response = response or {"entities": [], "relations": []}
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls.append(messages)
        if isinstance(self.response, str):
            return self.response
        return json.dumps(self.response, ensure_ascii=False)


class FailingLLM:
    """Mock LLM that always raises."""

    async def chat(self, messages: list[dict], **kwargs) -> str:
        raise RuntimeError("LLM unavailable")
