"""Test domain.yaml loading."""
import pytest


def _run(*args):
    """Run pytest via venv python with system pytest available."""
    import sys, subprocess
    cmd = [
        ".venv/bin/python", "-c",
        "import sys; sys.path.insert(0, '/home/ubuntu/.local/lib/python3.12/site-packages'); "
        "import pytest; sys.exit(pytest.main(" + str(list(args)) + "))"
    ]
    return subprocess.run(cmd, cwd="/home/ubuntu/projects/spiderweb",
                          env={**__import__("os").environ, "PYTHONPATH": "src"},
                          capture_output=True, text=True)


class TestDomainLoad:
    """RED→GREEN: domain config loads correctly."""

    def test_loads_name_and_version(self):
        """Domain config has correct name and version."""
        from spiderweb.engine.domain import load_domain
        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")
        assert config.name == "reading"
        assert config.version == "1.0"

    def test_loads_entity_types(self):
        """Domain config parses entity type tree."""
        from spiderweb.engine.domain import load_domain, get_entity_types_flat
        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")

        flat = get_entity_types_flat(config)
        assert "person" in flat
        assert "person.philosopher" in flat
        assert "concept" in flat
        assert "work" in flat

        # Verify labels
        assert config.entity_types["person"]["label"] == "人物"

    def test_loads_relation_types(self):
        """Domain config has relations with valid_triplets."""
        from spiderweb.engine.domain import load_domain
        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")
        rels = config.relation_types
        assert "AUTHORED" in rels
        assert rels["AUTHORED"]["label"] == "创作"
        assert ["person", "AUTHORED", "work"] in rels["AUTHORED"]["valid_triplets"]

    def test_prompts_dir_set(self):
        """Domain config sets prompts_dir when it exists."""
        from spiderweb.engine.domain import load_domain
        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")
        assert config.prompts_dir is not None
        assert "prompts" in config.prompts_dir


class TestLoadPrompt:
    """RED→GREEN: prompt templates load from domain pack."""

    def test_loads_existing_prompt(self):
        from spiderweb.engine.domain import load_domain, load_prompt
        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")
        content = load_prompt(config, "extract_entities.md")
        assert content is not None
        assert "structured knowledge" in content

    def test_returns_none_for_missing_prompt(self):
        from spiderweb.engine.domain import load_domain, load_prompt
        config = load_domain("/home/ubuntu/projects/spiderweb/domains/reading")
        assert load_prompt(config, "nonexistent.md") is None
