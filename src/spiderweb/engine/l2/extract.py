"""LLM-based entity and relation extraction from chunks."""
import json
import re
import asyncio
import sys
import time
import os
from ..providers import LLMProvider
from ..domain import DomainConfig, get_entity_types_flat
from ..hooks import run_validators

BATCH_TIMEOUT_S = 120    # per LLM call
OVERALL_TIMEOUT_S = 600  # entire extraction

def _log(msg: str):
    """Write to dedicated extract log, fallback to stderr."""
    log_path = os.path.expanduser("~/.spiderweb/reading/extract.log")
    try:
        with open(log_path, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        print(f"[spiderweb] {msg}")


async def _backoff(attempt: int):
    """Exponential backoff: 0.5s, 1s, 2s."""
    await asyncio.sleep(0.5 * (2 ** attempt))

EXTRACT_SYSTEM = """你从中文文本中提取知识图谱。文本含多个段落（用---分隔）。输出 JSON: {"entities":[{"name":"实体名","type":"类型","aliases":["别名"],"description":"一句话描述"}],"relations":[{"entity_a":"","entity_b":"","relation_type":""}]}。只提取明确出现的实体。"""

EXTRACT_USER = """实体类型: {entity_types}

关系类型: {relation_types}

文本:
---
{text}
---

输出 JSON: {{"entities":[{{"name":"实体名","type":"类型","aliases":["别名"],"description":"一句话描述"}}],"relations":[{{"entity_a":"","entity_b":"","relation_type":""}}]}}"""


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
                response = await asyncio.wait_for(
                    provider.chat([
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": prompt},
                    ]),
                    timeout=BATCH_TIMEOUT_S,
                )
                if not response or not response.strip():
                    await _backoff(attempt)
                    continue
                data = _parse_json(response)
                if data and "entities" in data:
                    return data.get("entities", []), data.get("relations", [])
            except asyncio.TimeoutError:
                _log(f"extractbatch timed out ({BATCH_TIMEOUT_S}s), attempt {attempt+1}")
            except Exception as e:
                _log(f"extractbatch error: {e}")
            await _backoff(attempt)
        _log(f"extractbatch FAILED after 3 attempts")
        return None, None

    # Adaptive batching: exponential concurrency 1→3→9→...
    # Wave 1 warms prompt cache, then scale up. Target ~15K chars/batch.
    MAX_CHARS_PER_CALL = 15_000

    # Build prompts with char-limit splitting
    prompts = []  # list of (prompt_text, chunk_count, char_count)
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        current_group, current_chars = [], 0
        for cid, text in batch:
            text_len = len(text)
            if current_chars + text_len > MAX_CHARS_PER_CALL and current_group:
                combined = "\n\n---\n\n".join(t for _, t in current_group)
                prompts.append((combined, len(current_group), current_chars))
                current_group, current_chars = [], 0
            current_group.append((cid, text))
            current_chars += text_len
        if current_group:
            combined = "\n\n---\n\n".join(t for _, t in current_group)
            prompts.append((combined, len(current_group), current_chars))

    _log(f"extractconcurrent: {len(prompts)} batches from {len(chunks)} chunks")

    from ..providers import OpenAILLM
    _fallback_llm = OpenAILLM(model="deepseek-v4-pro", base_url="https://api.deepseek.com/v1")

    async def _process_one(prompt_text: str, idx: int):
        """Process one prompt: primary LLM → fallback if needed."""
        full_prompt = user_template.format(
            entity_types=entity_types_str,
            relation_types=relation_types_str,
            text=prompt_text,
        )
        entities, relations = await _extract_batch(llm, full_prompt)
        if entities is None:
            entities, relations = await _extract_batch(_fallback_llm, full_prompt)
        result = entities if entities else []
        _log(f"extractdone batch {idx+1}/{len(prompts)}: {len(result)} entities")
        return result, relations if relations else []

    # Exponential concurrency: 1→3→9→... warms prompt cache, then full speed
    results = [None] * len(prompts)
    t_start = time.time()
    offset = 0
    wave = 1  # 1, 3, 9, 27...

    while offset < len(prompts):
        count = min(wave, len(prompts) - offset)
        batch = range(offset, offset + count)
        _log(f"extractwave {wave}: {count} batches (offset={offset})")

        tasks = [_process_one(prompts[i][0], i) for i in batch]
        wave_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=OVERALL_TIMEOUT_S,
        )
        for i, r in zip(batch, wave_results):
            results[i] = r

        offset += count
        wave *= 3

    _log(f"extractall done in {time.time()-t_start:.0f}s")

    for r in results:
        if isinstance(r, Exception):
            _log(f"extractbatch exception: {r}")
            continue
        if isinstance(r, tuple) and len(r) == 2:
            all_entities.extend(r[0])
            all_relations.extend(r[1])

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
