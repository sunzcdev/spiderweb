"""L3 insight — 文件为真水源，DB 为派生搜索索引。

写入路径: 写 Markdown 文件 → 更新 DB (FTS5 + 向量 + 实体链接)
同步路径: 扫描 insights/*.md → 对比 mtime → 增量重建 DB
"""
import json, re, os, hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..providers import (
    create_llm_provider, create_embedding_provider,
    create_tokenizer_provider,
)
from ..domain import DomainConfig, get_entity_types_flat

# ── Markdown frontmatter helpers ──────────────────────────────────

_FM_SEP = "---"


def _read_md(path: str) -> dict | None:
    """Parse one Obsidian-compatible insight .md file.

    Returns dict with slug, title, content, created_at, updated_at,
    source_docs, entities, file_mtime, or None if parse failed.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    mtime = os.path.getmtime(path)
    fm, body = _split_frontmatter(text)
    if fm is None:
        return None

    slug = _fm_get(fm, "slug") or Path(path).stem
    return {
        "slug": slug,
        "title": _fm_get(fm, "title") or slug,
        "content": body or "",
        "created_at": _fm_get(fm, "created_at") or _iso_now(),
        "updated_at": _fm_get(fm, "updated_at") or _iso_now(),
        "source_docs": _fm_get(fm, "source_docs", []) or [],
        "entities": _fm_get(fm, "entities", []) or [],
        "file_mtime": mtime,
    }


def _write_md(path: str, slug: str, title: str, content: str,
              source_docs: list[str] | None = None,
              entities: list[str] | None = None):
    """Write one insight as an Obsidian-compatible Markdown file."""
    now = _iso_now()
    lines = [_FM_SEP,
             f"title: {title}",
             f"slug: {slug}",
             f"created_at: {now}",
             f"updated_at: {now}"]
    if source_docs:
        lines.append("source_docs:")
        for d in source_docs:
            lines.append(f"  - {d}")
    else:
        lines.append("source_docs: []")
    if entities:
        lines.append("entities:")
        for e in entities:
            lines.append(f"  - {e}")
    else:
        lines.append("entities: []")
    lines += [_FM_SEP, "", content.strip(), ""]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split '---\\n...\\n---\\nbody' into (frontmatter_dict, body)."""
    text = text.strip()
    if not text.startswith(_FM_SEP):
        return None, text
    rest = text[3:].strip()
    end = rest.find("\n" + _FM_SEP)
    if end == -1:
        return None, text
    fm_block = rest[:end].strip()
    body = rest[end + 4:].strip()

    fm = {}
    cur_key = None
    for line in fm_block.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            val = s[2:].strip()
            if cur_key is not None:
                if not isinstance(fm.get(cur_key), list):
                    fm[cur_key] = []
                fm[cur_key].append(val)
            continue
        cur_key = None
        if ":" not in s:
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        fm[k] = v
        if v == "" or v == "[]":
            cur_key = k
            if v == "[]":
                fm[k] = []
    return fm, body


def _fm_get(fm: dict, key: str, default=None):
    """Get frontmatter field, handling both string and list types."""
    v = fm.get(key, default)
    # source_docs/entities stored as list from yaml-like parsing
    return v


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── DB index helpers (reused from original write_insight) ─────────

async def _index_in_db(db, config: DomainConfig, slug: str, title: str,
                       content: str, source_docs: list[str]) -> dict:
    """Index one insight into DB: insights table, FTS5, vector, entity linking.

    This is the 'DB as derived index' step — everything here can be
    rebuilt from the Markdown file via sync_insights().
    """
    from ..l1.ingest import ensure_vec_table

    source_docs = source_docs or []

    # Upsert insights table
    existing = db.execute(
        "SELECT id FROM insights WHERE slug = ?", (slug,)
    ).fetchone()

    if existing:
        insight_id = existing[0]
        db.execute(
            "UPDATE insights SET title = ?, content = ?, source_docs_json = ?, updated_at = datetime('now') "
            "WHERE slug = ?",
            (title, content, json.dumps(source_docs, ensure_ascii=False), slug)
        )
    else:
        cursor = db.execute(
            "INSERT INTO insights (slug, title, content, source_docs_json, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (slug, title, content, json.dumps(source_docs, ensure_ascii=False))
        )
        insight_id = cursor.lastrowid

    # FTS5 — triggers handle sync automatically
    tokenizer = create_tokenizer_provider(config.tokenizer)
    if tokenizer:
        tokenizer.tokenize(title)
        tokenizer.tokenize(content)

    # Vector index
    vec_count = 0
    emb_provider = create_embedding_provider(config.embedding)
    if emb_provider:
        ensure_vec_table(db, emb_provider.dimensions, "insights_vec")
        try:
            vecs = await emb_provider.embed([f"{title}\n{content}"])
            if existing:
                db.execute("DELETE FROM insights_vec WHERE rowid = ?", (insight_id,))
            db.execute(
                "INSERT INTO insights_vec(rowid, embedding) VALUES (?, ?)",
                (insight_id, json.dumps(vecs[0]))
            )
            vec_count = 1
        except Exception as e:
            import sys
            print(f"[spiderweb] Failed to embed insight {insight_id}: {e}", file=sys.stderr)

    # Reverse extract entities + link to L2
    link_result = {}
    try:
        llm = create_llm_provider(config.llm)
        entities, relations = await _reverse_extract(llm, config, content)
        link_result = _link_insight_to_entities(db, config, insight_id, entities, relations)
    except Exception as e:
        link_result = {"error": str(e)}

    db.commit()
    return {
        "ok": True, "insight_id": insight_id, "slug": slug,
        "vectors": vec_count,
        "entities_extracted": link_result.get("entities_extracted", 0),
        "entities_registered": link_result.get("entities_registered", 0),
        "edges_added": link_result.get("edges_added", 0),
        "entity_names": link_result.get("entity_names", []),
    }


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

async def write_insight(db, config: DomainConfig, title: str,
                        content: str, source_docs: list[str] | None = None) -> dict:
    """Write insight: Markdown file (truth) → DB index (derived)."""
    if not content or not content.strip():
        return {"ok": False, "error": "content is empty"}

    # Generate slug
    content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
    slug_base = re.sub(r'[^a-z0-9一-鿿]+', '-', title.lower().strip())[:60]
    slug = f"{slug_base}-{content_hash}"

    # Resolve output path
    insights_dir = _insights_dir(config)
    filepath = os.path.join(insights_dir, f"{slug}.md")

    # If file already exists and content hash matches — it's a duplicate
    if os.path.exists(filepath):
        existing = _read_md(filepath)
        if existing and existing["slug"] == slug:
            return {"ok": True, "slug": slug, "note": "already exists (duplicate content)"}

    # 1. Write Markdown file (source of truth)
    _write_md(filepath, slug, title, content, source_docs or [])

    # 2. Update DB index (derived — best-effort, failure won't lose data)
    try:
        idx_result = await _index_in_db(db, config, slug, title, content, source_docs or [])
    except Exception as e:
        idx_result = {"error": str(e)}

    result = {"ok": True, "slug": slug, "file": filepath}
    result.update(idx_result)
    return result


async def sync_insights(db, config: DomainConfig) -> dict:
    """Scan insights/*.md, re-index changed/new files into DB.

    Idempotent. Uses file mtime vs DB updated_at to skip unchanged files.
    Returns stats dict.
    """
    insights_dir = _insights_dir(config)
    if not os.path.isdir(insights_dir):
        return {"ok": False, "error": f"insights_dir not found: {insights_dir}"}

    stats = {"checked": 0, "added": 0, "updated": 0, "skipped": 0, "errors": 0}

    for fname in sorted(os.listdir(insights_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(insights_dir, fname)
        parsed = _read_md(fpath)
        if parsed is None:
            stats["errors"] += 1
            continue
        stats["checked"] += 1

        slug = parsed["slug"]
        row = db.execute(
            "SELECT updated_at FROM insights WHERE slug = ?", (slug,)
        ).fetchone()
        db_mtime = row[0] if row else ""

        if db_mtime and parsed["file_mtime"] <= _parse_db_time(db_mtime):
            stats["skipped"] += 1
            continue

        try:
            result = await _index_in_db(db, config, parsed["slug"], parsed["title"],
                                        parsed["content"], parsed["source_docs"])
            if result.get("ok"):
                stats["added" if not row else "updated"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

    return stats


# ═══════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════

def _insights_dir(config: DomainConfig) -> str:
    """Resolve the insights output directory."""
    if config.insights_dir:
        return os.path.expanduser(config.insights_dir)
    return os.path.join(config.data_dir, "insights")


def _parse_db_time(s: str) -> float:
    """Parse SQLite datetime string → unix timestamp (float)."""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except ValueError:
        return 0


# ── Reverse extract (LLM entity extraction) ─────────────────────

_REVERSE_EXTRACT_SYSTEM = """You extract entities and relations from a user's personal note/insight.
Output ONLY valid JSON.

Given a note, extract:
1. entities: list of {{"name": "canonical name", "type": "entity_type"}}
2. relations: list of {{"entity_a": "name", "entity_b": "name", "relation_type": "MENTIONS"}}

Rules:
- Entity types must be from the provided list
- Only extract entities EXPLICITLY mentioned in the note
- Use the most standard canonical form for names
- All relations should use "MENTIONS" type (the note mentions these entities together)
- Include entities that are the SUBJECT of the note, not just passing mentions"""


async def _reverse_extract(llm, config: DomainConfig, content: str):
    """Extract entities and relations from insight content via LLM."""
    from ..domain import load_prompt

    entity_types_str = ", ".join(sorted(get_entity_types_flat(config)))
    prompt = (
        "Entity types available:\n{entity_types}\n\n"
        "User's note:\n---\n{content}\n---\n\n"
        'Output JSON with "entities" and "relations" arrays.\n'
        '{{"entities": [...], "relations": [...]}}'
    ).format(entity_types=entity_types_str, content=content)

    sys_prompt = (load_prompt(config, "record_insight.md") or _REVERSE_EXTRACT_SYSTEM)

    async def _try():
        for attempt in range(3):
            try:
                import asyncio
                resp = await llm.chat([
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ])
                if not resp or not resp.strip():
                    continue
                data = _parse_json(resp)
                if data and "entities" in data:
                    return data.get("entities", []), data.get("relations", [])
            except Exception:
                pass
            await asyncio.sleep(0.5 * (2 ** attempt))
        return None

    result = await _try()
    return result or ([], [])


def _link_insight_to_entities(db, config, insight_id, entities, relations):
    """Register extracted entities, build edges, update traces."""
    registered = 0
    linked = 0
    entity_names = []

    for e in entities:
        name = e.get("name", "").strip()
        etype = e.get("type", "concept")
        if not name:
            continue
        entity_names.append((name, etype))

        existing = db.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (name,)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO entities (canonical_name, entity_type, source, aliases_json) "
                "VALUES (?, ?, 'l3_insight', '[]')", (name, etype)
            )
            registered += 1

    # MENTIONS edges from insight to each entity
    insight_slug = db.execute("SELECT slug FROM insights WHERE id = ?", (insight_id,)).fetchone()
    if insight_slug:
        for name, _ in entity_names:
            if _ensure_relation(db, insight_slug[0], name, "MENTIONS", 1.0):
                linked += 1

    # Entity co-occurrence edges
    all_rels = list(relations)
    for i, (a, _) in enumerate(entity_names):
        for b, _ in entity_names[i + 1:]:
            all_rels.append({"entity_a": a, "entity_b": b, "relation_type": "MENTIONS"})

    for r in all_rels:
        a, b, rtype = r.get("entity_a", ""), r.get("entity_b", ""), r.get("relation_type", "MENTIONS")
        if a and b:
            if _ensure_relation(db, a, b, rtype, 1.0):
                linked += 1

    # Traces
    for name, _ in entity_names:
        db.execute(
            "INSERT INTO entity_traces (entity_name, source, count, last_seen) "
            "VALUES (?, 'l3_insight', 1, datetime('now')) "
            "ON CONFLICT(entity_name, source) DO UPDATE SET count = count + 1, last_seen = datetime('now')",
            (name,)
        )

    # Update insight's entities_json
    db.execute(
        "UPDATE insights SET entities_json = ? WHERE id = ?",
        (json.dumps([n for n, _ in entity_names], ensure_ascii=False), insight_id)
    )

    # Update cross_doc_count
    for name, _ in entity_names:
        count = db.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE body LIKE ?",
            (f"%{name}%",)
        ).fetchone()[0]
        db.execute(
            "UPDATE entities SET cross_doc_count = ? WHERE canonical_name = ?",
            (count, name)
        )

    db.commit()
    return {
        "entities_extracted": len(entity_names),
        "entities_registered": registered,
        "edges_added": linked,
        "entity_names": [n for n, _ in entity_names],
    }


def _ensure_relation(db, a, b, rtype, weight):
    """Insert relation if not exists. Returns True if added."""
    if a == b:
        return False
    existing = db.execute(
        "SELECT id FROM relations WHERE entity_a = ? AND entity_b = ? AND relation_type = ?",
        (a, b, rtype)
    ).fetchone()
    if existing:
        return False
    db.execute(
        "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?, ?, ?, ?)",
        (a, b, rtype, weight)
    )
    return True


def _parse_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── Backward-compat aliases for existing tests ───────────────

reverse_extract = _reverse_extract
link_insight_to_entities = _link_insight_to_entities
