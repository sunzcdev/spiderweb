"""LLM-based entity and relation extraction from chunks."""
import json
import re
import asyncio
from ..providers import LLMProvider
from ..domain import DomainConfig, get_entity_types_flat
from ..hooks import run_validators


async def _backoff(attempt: int):
    """Exponential backoff: 0.5s, 1s, 2s."""
    await asyncio.sleep(0.5 * (2 ** attempt))

EXTRACT_SYSTEM = """You extract structured knowledge from text. Output ONLY valid JSON.

Given a text passage, extract:
1. entities: list of {{"name": "canonical name", "type": "entity_type", "aliases": ["other names"], "description": "one sentence in the source language"}}
2. relations: list of {{"entity_a": "name", "entity_b": "name", "relation_type": "REL_TYPE"}}

Rules:
- Canonical names should be the most standard form (e.g., "Confucius" not "Kong Qiu")
- Entity types must be from the provided list
- Relation types must be from the provided list
- Only extract entities and relations that are EXPLICITLY mentioned in the text
- Do NOT invent entities or relations not supported by the text
- Merge co-referring entities (same person/concept with different names → use aliases)"""

EXTRACT_USER = """Entity types available:
{entity_types}

Relation types available:
{relation_types}

Valid relation patterns:
{valid_triplets}

Text passage:
---
{text}
---

Output JSON with "entities" and "relations" arrays.
{{"entities": [...], "relations": [...]}}"""


def _format_types(config: DomainConfig) -> str:
    types = get_entity_types_flat(config)
    return ", ".join(sorted(types))


def _format_relations(config: DomainConfig) -> str:
    lines = []
    for name, info in config.relation_types.items():
        label = info.get("label", name) if isinstance(info, dict) else name
        triplets = info.get("valid_triplets", []) if isinstance(info, dict) else []
        triplet_strs = [f"({s} → {r} → {o})" for s, r, o in triplets] if triplets else ["(any → any)"]
        lines.append(f"- {name} ({label}): {', '.join(triplet_strs)}")
    return "\n".join(lines)


def _format_triplets(config: DomainConfig) -> str:
    lines = []
    for name, info in config.relation_types.items():
        if not isinstance(info, dict):
            continue
        for s, r, o in info.get("valid_triplets", []):
            lines.append(f"- ({s}) --[{r}]--> ({o})")
    return "\n".join(lines) if lines else "Any entity type can have any relation."


async def extract_from_chunks(
    llm: LLMProvider,
    config: DomainConfig,
    chunks: list[tuple[int, str]],  # [(chunk_id, text), ...]
    batch_size: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Extract entities and relations from chunks. Returns (entities, relations)."""
    from ..domain import load_prompt

    all_entities = []
    all_relations = []
    entity_types_str = _format_types(config)
    relation_types_str = _format_relations(config)
    valid_triplets_str = _format_triplets(config)

    sys_prompt = load_prompt(config, "extract_entities.md") or EXTRACT_SYSTEM
    user_template = load_prompt(config, "extract_relations.md") or EXTRACT_USER

    # Append domain-specific description template as formatting guidance
    if config.entity_description_template:
        sys_prompt += "\n\nFor entity descriptions, follow this format:\n" + config.entity_description_template

    # Fallback model if primary returns empty (deepseek-v4-flash rate limit)
    _fallback_llm = None

    async def _extract_batch(provider, prompt):
        for attempt in range(3):
            try:
                response = await provider.chat([
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ])
                if not response or not response.strip():
                    await _backoff(attempt)
                    continue
                data = _parse_json(response)
                if data and "entities" in data:
                    return data.get("entities", []), data.get("relations", [])
            except Exception:
                pass
            await _backoff(attempt)
        return None, None

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        combined_text = "\n\n---\n\n".join(text for _, text in batch)

        prompt = user_template.format(
            entity_types=entity_types_str,
            relation_types=relation_types_str,
            valid_triplets=valid_triplets_str,
            text=combined_text,
        )

        entities, relations = await _extract_batch(llm, prompt)
        if entities is None:
            # Fallback to deepseek-chat
            if _fallback_llm is None:
                from ..providers import OpenAILLM
                _fallback_llm = OpenAILLM(model="deepseek-chat", base_url="https://api.deepseek.com/v1")
            entities, relations = await _extract_batch(_fallback_llm, prompt)

        if entities:
            all_entities.extend(entities)
            all_relations.extend(relations)

    # Deduplicate entities by canonical name
    seen = {}
    for e in all_entities:
        name = e.get("name", "").strip()
        if not name:
            continue
        if name in seen:
            # Merge aliases
            existing = seen[name]
            existing_aliases = set(existing.get("aliases", []))
            existing_aliases.update(e.get("aliases", []))
            existing["aliases"] = list(existing_aliases)
        else:
            seen[name] = e

    # Validate and deduplicate relations
    valid_entity_names = set(seen.keys())
    unique_relations = {}
    for r in all_relations:
        a, b, rtype = r.get("entity_a", "").strip(), r.get("entity_b", "").strip(), r.get("relation_type", "").strip()
        if not a or not b or not rtype:
            continue
        if not _validate_triplet(a, b, rtype, config, valid_entity_names, seen):
            continue
        key = (a, b, rtype)
        if key not in unique_relations:
            unique_relations[key] = {"entity_a": a, "entity_b": b, "relation_type": rtype}

    return list(seen.values()), list(unique_relations.values())


def _validate_triplet(a: str, b: str, rtype: str, config: DomainConfig, valid_names: set[str], entities: dict) -> bool:
    """Check if (a, rtype, b) is valid per domain schema."""
    if a not in valid_names or b not in valid_names:
        return False
    rel_info = config.relation_types.get(rtype)
    if not rel_info:
        return False
    triplets = rel_info.get("valid_triplets", []) if isinstance(rel_info, dict) else []
    if not triplets:
        return True  # No restrictions — MENTIONS style

    a_type = entities.get(a, {}).get("type", "").split(".")[0]  # base type
    b_type = entities.get(b, {}).get("type", "").split(".")[0]

    # Static schema check
    schema_ok = False
    for s, _, o in triplets:
        if (s == a_type or s == a_type.split(".")[0]) and (o == b_type or o == b_type.split(".")[0]):
            schema_ok = True
            break
    if not schema_ok:
        return False

    # Custom validators from hooks
    ctx = {"entity_a": a, "entity_b": b, "entities": entities}
    if not run_validators(config.hooks_module, a_type, rtype, b_type, ctx):
        return False

    return True


def _parse_json(text: str) -> dict | None:
    """Extract JSON from LLM response."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try markdown code block
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try to find JSON object
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
