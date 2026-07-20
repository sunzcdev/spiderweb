"""Reading domain hooks — pre_ingest cleaners, post_extract rule engine, custom validators."""
import re
from spiderweb.engine.hooks import DomainHooks


class ReadingHooks(DomainHooks):
    """Hooks for the reading domain."""

    def pre_ingest(self):
        """Chain: strip EPUB artifacts, normalize whitespace."""
        return [
            _clean_epub_artifacts,
            _normalize_whitespace,
        ]

    def post_extract(self):
        """Chain: noise filtering to keep graph clean."""
        return [
            _filter_noise_entities,
        ]

    def validators(self):
        """Chain: custom validation rules."""
        return []


# === Pre-ingest hooks ===

def _clean_epub_artifacts(text: str, meta: dict) -> str:
    """Strip common EPUB/calibre artifacts."""
    # calibre HTML class markers: {.calibreN}, {.calibreNN}
    text = re.sub(r'(?i)\{\.calibre\d+\}', '', text)
    # ISBN / word-count metadata lines
    text = re.sub(r'(?im)^(?:ISBN|字数|Word Count)[:\s].*$', '', text)
    # Image references: ![alt](...) and empty links [](#...)
    text = re.sub(r'(?i)!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'(?i)\[\]\(.*?\)', '', text)
    # Telegram / channel subscription ads
    text = re.sub(r'(?im)(?:关注|加入|订阅)\s*.{0,30}(?:频道|群组|telegram|公众号).{0,80}', '', text)
    return text


def _normalize_whitespace(text: str, meta: dict) -> str:
    """Normalize whitespace: collapse multiple blank lines."""
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# === Post-extract hooks ===

# Noise patterns: entity names matching these patterns are filtered out.
# Each is a compiled regex tested against entity name and description.
_NOISE_PATTERNS = [
    # Generic infrastructure / system / process terms
    re.compile(r'(?:数据|信息|管理|处理|控制|操作|运行|维护|支持)(?:系统|平台|中心|方式|方法|流程|机制)'),
    # English "Last, First" citation format
    re.compile(r'^[A-Z][a-z]+,\s*[A-Z][a-z]+$'),
    # Pure generic terms (short, no specific knowledge value)
    re.compile(r'^(?:问题|方法|因素|条件|情况|手段|途径|方式|过程|步骤|阶段|环节|方面|内容|形式|状态|特征|特点|类型|种类|模式)$'),
    # Generic organizational references
    re.compile(r'(?:部门|小组|团队|委员会|理事会|协会|组织)$'),
]


def _filter_noise_entities(
    entities: list[dict],
    relations: list[dict],
    doc_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Remove noise entities that don't belong in the knowledge graph.

    Filters based on:
    - Known noise patterns (generic systems, citation formats, etc.)
    - Entity names that reference entities from clearly unrelated domains
      without sufficient context
    """
    noise_names = set()

    for ent in entities:
        name = ent.get("name", "").strip()
        etype = ent.get("type", "")
        if _is_noise_entity(name, etype):
            noise_names.add(name)

    if not noise_names:
        return entities, relations

    filtered_entities = [e for e in entities if e.get("name", "").strip() not in noise_names]
    filtered_relations = [
        r for r in relations
        if r.get("entity_a", "").strip() not in noise_names
        and r.get("entity_b", "").strip() not in noise_names
    ]

    return filtered_entities, filtered_relations


def _is_noise_entity(name: str, etype: str) -> bool:
    """Check if an entity should be filtered out as noise."""
    if not name:
        return True

    # Check against noise patterns
    for pattern in _NOISE_PATTERNS:
        if pattern.search(name):
            return True

    # Single-character names are almost always noise
    if len(name) <= 1:
        return True

    return False
