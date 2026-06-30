"""Domain pack loader — reads domain.yaml, config.yaml, prompts, hooks."""
import os
import yaml
import importlib.util
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class DomainConfig:
    name: str = ""
    version: str = "1.0"
    entity_types: dict = field(default_factory=dict)
    relation_types: dict = field(default_factory=dict)
    doc_meta_schema: dict = field(default_factory=dict)
    entity_description_template: str = ""

    # Runtime config
    data_dir: str = ""
    chunk_size: int = 1500
    chunk_overlap: int = 150
    extraction_mode: str = "sample"  # full | sample
    extraction_sample_rate: float = 0.3

    # Provider configs
    llm: dict = field(default_factory=dict)
    embedding: dict = field(default_factory=dict)
    tokenizer: dict = field(default_factory=dict)
    reranker: dict = field(default_factory=dict)

    # Paths
    prompts_dir: str = ""
    hooks_module: object = None


def load_domain(domain_path: str) -> DomainConfig:
    """Load a domain pack from a directory path."""
    import sys
    dp = Path(domain_path)
    if not dp.is_dir():
        raise FileNotFoundError(f"Domain path not found: {domain_path}")

    config = DomainConfig()

    # Load domain.yaml
    domain_yaml = dp / "domain.yaml"
    if domain_yaml.exists():
        with open(domain_yaml) as f:
            raw = yaml.safe_load(f) or {}
        config.name = raw.get("name", dp.name)
        config.version = raw.get("version", "1.0")
        config.entity_types = raw.get("entity_types", {})
        config.relation_types = raw.get("relation_types", {})
        config.doc_meta_schema = raw.get("doc_meta_schema", {})
        config.entity_description_template = raw.get("entity_description_template", "")
    else:
        config.name = dp.name
        print(f"[spiderweb] WARNING: no domain.yaml found at {domain_path} — "
              "this may not be a domain pack directory. "
              "SPIDERWEB_DOMAIN should point to a directory with domain.yaml + config.yaml.", file=sys.stderr)

    # Load config.yaml (runtime config)
    config_yaml = dp / "config.yaml"
    if config_yaml.exists():
        with open(config_yaml) as f:
            raw = yaml.safe_load(f) or {}
        config.data_dir = os.path.expanduser(raw.get("data_dir", f"~/.spiderweb/{config.name}"))
        config.chunk_size = raw.get("chunk_size", 1500)
        config.chunk_overlap = raw.get("chunk_overlap", 150)
        config.extraction_mode = raw.get("extraction", {}).get("mode", "sample")
        config.extraction_sample_rate = raw.get("extraction", {}).get("sample_rate", 0.3)
        config.llm = raw.get("providers", {}).get("llm", {})
        config.embedding = raw.get("providers", {}).get("embedding", {})
        config.tokenizer = raw.get("providers", {}).get("tokenizer", {})
        config.reranker = raw.get("providers", {}).get("reranker", {})
    else:
        config.data_dir = os.path.expanduser(f"~/.spiderweb/{config.name}")
        print(f"[spiderweb] WARNING: no config.yaml found at {domain_path} — "
              "providers (LLM/embedding/tokenizer/reranker) will NOT be configured. "
              "Chinese FTS5 search will not work without a tokenizer. "
              "Set SPIDERWEB_DOMAIN to the domain pack directory (e.g. .../domains/reading/).", file=sys.stderr)

    # Prompt templates
    prompts_dir = dp / "prompts"
    config.prompts_dir = str(prompts_dir) if prompts_dir.is_dir() else ""

    # Optional hooks.py
    hooks_py = dp / "hooks.py"
    if hooks_py.exists():
        spec = importlib.util.spec_from_file_location("domain_hooks", str(hooks_py))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        config.hooks_module = mod

    return config


def load_prompt(config: DomainConfig, name: str) -> str | None:
    """Load a prompt template from the domain's prompts/ directory."""
    if not config.prompts_dir:
        return None
    path = os.path.join(config.prompts_dir, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def get_entity_types_flat(config: DomainConfig) -> set[str]:
    """Flatten entity type tree to a set of all valid types (including subtypes like person.ruler)."""
    types = set()

    def walk(tree, prefix=""):
        for name, info in tree.items():
            full = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
            types.add(full)
            subtypes = info.get("subtypes", {}) if isinstance(info, dict) else {}
            if subtypes:
                walk(subtypes, full)

    walk(config.entity_types)
    return types
