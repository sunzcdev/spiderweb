"""Document ingestion — parse, chunk, store, vectorize."""
import json
import os
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from ..providers import EmbeddingProvider
from ..hooks import run_pre_ingest


@dataclass
class Chunk:
    section_path: str
    heading_level: int
    body: str
    line_start: int


def ingest_file(file_path: str, chunk_size: int = 1500, chunk_overlap: int = 150,
                hooks_module=None) -> tuple[str, str, list[Chunk]]:
    """Ingest a file, return (title, author, list of Chunks). Supports .md, .txt, .epub."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".epub":
        text = _epub_to_markdown(str(path))
        title = _extract_title(text) or path.stem
        author = ""
    elif suffix == ".md":
        text = path.read_text(encoding="utf-8")
        title = _extract_title(text) or path.stem
        author = ""
    elif suffix == ".txt":
        text = path.read_text(encoding="utf-8")
        title = path.stem
        author = ""
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    # Run pre_ingest hooks (chainable: each transforms text)
    meta = {"file_path": file_path, "title": title, "author": author}
    text = run_pre_ingest(hooks_module, text, meta)

    # Chunk after hooks (hooks may alter content)
    if suffix in (".epub", ".md"):
        chunks = _chunk_markdown(text)
    else:
        chunks = _chunk_sliding_window(text, chunk_size, chunk_overlap)

    return title, author, chunks


def _extract_title(text: str) -> str | None:
    """Extract title from first # heading or first line."""
    for line in text.split("\n")[:20]:
        m = re.match(r"^#\s+(.+)", line)
        if m:
            return m.group(1).strip()
    first_line = text.split("\n")[0].strip()
    return first_line[:200] if first_line else None


def _chunk_markdown(text: str) -> list[Chunk]:
    """Chunk markdown by #, ##, ### headings. Each heading splits into its own chunk."""
    chunks = []
    # Split on any ATX heading (#, ##, ###) at start of a line
    sections = re.split(r"\n(?=#{1,3}\s)", text)
    line_offset = 0

    for section in sections:
        lines = section.split("\n")
        heading_match = re.match(r"^(#{1,3})\s+(.+)", lines[0]) if lines else None

        if heading_match:
            level = len(heading_match.group(1))  # 1 for #, 2 for ##, 3 for ###
            section_path = heading_match.group(2).strip()
            body = "\n".join(lines[1:]).strip()
        else:
            level = 0
            section_path = ""
            body = section.strip()

        if not body:
            line_offset += len(lines)
            continue

        chunks.append(Chunk(
            section_path=section_path,
            heading_level=level,
            body=body,
            line_start=line_offset,
        ))
        line_offset += len(lines)

    return chunks


def _chunk_sliding_window(text: str, chunk_size: int, overlap: int) -> list[Chunk]:
    """Sliding window chunking for plain text."""
    chunks = []
    text = text.strip()
    pos = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        # Try to break at paragraph boundary
        if end < len(text):
            para_break = text.rfind("\n\n", pos, end)
            if para_break > pos + chunk_size // 2:
                end = para_break
        body = text[pos:end].strip()
        if body:
            chunks.append(Chunk(
                section_path="",
                heading_level=0,
                body=body,
                line_start=pos,
            ))
        pos = end - overlap if end < len(text) else len(text)
    return chunks


def _epub_to_markdown(file_path: str) -> str:
    """Convert epub to markdown via pandoc."""
    try:
        result = subprocess.run(
            ["pandoc", file_path, "-t", "markdown", "--wrap=none"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: extract from zip
    import zipfile
    with zipfile.ZipFile(file_path) as zf:
        for name in zf.namelist():
            if name.endswith((".html", ".xhtml", ".htm")) and "nav" not in name.lower():
                html = zf.read(name).decode("utf-8", errors="replace")
                # Crude HTML-to-text: strip tags
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()
                return text
    return ""


# === Vector indexing ===

def ensure_vec_table(db, dimensions: int, table_name: str = "chunks_vec"):
    """Create vec0 table if it doesn't exist. Must match stored dimensions."""
    # sqlite_vec already loaded by get_db()
    meta_key = f'vec_dim_{table_name}' if table_name != 'chunks_vec' else 'embedding_dimensions'
    existing = db.execute(f"SELECT value FROM _meta WHERE key = ?", (meta_key,)).fetchone()
    if existing:
        stored_dim = int(existing[0])
        if stored_dim != dimensions:
            raise ValueError(
                f"Embedding dimensions mismatch for {table_name}: stored {stored_dim}, configured {dimensions}. "
                "Cannot change embedding model after graph creation."
            )
        return  # Already initialized

    db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0(embedding float[{dimensions}])")
    db.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)", (meta_key, str(dimensions)))
    if table_name == 'chunks_vec':
        db.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('has_vectors', '1')")
    else:
        db.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES (?, '1')",
                   (f'has_vectors_{table_name}',))
    db.commit()


async def index_vectors(db, provider: EmbeddingProvider, chunk_ids: list[int],
                       table_name: str = "chunks_vec", text_getter=None) -> int:
    """Embed texts and insert into vec0 table. Returns count indexed."""
    if not chunk_ids:
        return 0

    ensure_vec_table(db, provider.dimensions, table_name)
    count = 0

    # Default text getter: read from chunks table
    if text_getter is None:
        def text_getter(ids):
            rows = db.execute(
                f"SELECT id, body FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
                ids
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    # Token limit: voyage-4-large max 120K input tokens per batch.
    # Chinese: 1 token ≈ 1.5-2 chars. Safe limit: 80K chars ≈ 50K tokens.
    # Per-text limit: voyage-4-large ~32K tokens ≈ ~55K chars. Safe: 30K chars.
    MAX_CHARS_PER_BATCH = 80_000
    MAX_CHARS_PER_TEXT = 30_000  # Safety truncation for individual oversized chunks
    MIN_CHARS_PER_TEXT = 10  # Skip empty/trivial chunks that poison voyage batches

    batch_size = 32
    for i in range(0, len(chunk_ids), batch_size):
        batch = chunk_ids[i:i + batch_size]
        id_text_pairs = text_getter(batch)
        if not id_text_pairs:
            continue

        # Split large batches to stay under token limit
        sub_batches = []
        current_ids, current_texts, current_chars = [], [], 0
        for rid, text in id_text_pairs:
            # Truncate individual oversized texts to stay under voyage per-text limit
            if len(text) > MAX_CHARS_PER_TEXT:
                text = text[:MAX_CHARS_PER_TEXT]
            # Skip empty/trivial chunks that poison voyage batches
            if len(text.strip()) < MIN_CHARS_PER_TEXT:
                continue
            text_len = len(text)
            if current_chars + text_len > MAX_CHARS_PER_BATCH and current_texts:
                sub_batches.append((current_ids, current_texts))
                current_ids, current_texts, current_chars = [], [], 0
            current_ids.append(rid)
            current_texts.append(text)
            current_chars += text_len
        if current_texts:
            sub_batches.append((current_ids, current_texts))

        for ids, texts in sub_batches:
            try:
                embeddings = await provider.embed(texts)
            except Exception as e:
                print(f"[spiderweb] embedding batch failed ({len(ids)} chunks, {sum(len(t) for t in texts)} chars): {e}", file=__import__('sys').stderr)
                continue

            for rowid, vec in zip(ids, embeddings):
                db.execute(
                    f"INSERT OR REPLACE INTO {table_name}(rowid, embedding) VALUES (?, ?)",
                    (rowid, json.dumps(vec))
                )
                count += 1

    db.commit()
    return count


async def hybrid_search(db, query: str, query_vec: list[float] | None, top_n: int = 5,
                  tokenizer=None, reranker=None, body_max_len: int | None = 500) -> list[dict]:
    """Hybrid search: FTS5 + LIKE + vector → dedup → rerank → top_n."""
    results = []
    seen = set()

    # Text search: combine FTS5 (word-level precision) + LIKE (substring recall)
    fts_rows = []
    like_rows = []

    if tokenizer:
        tokenized_query = tokenizer.tokenize(query)
        tokens = tokenized_query.split()
        fts_query = " ".join(f"{t}*" for t in tokens)
        try:
            fts_rows = db.execute(
                "SELECT c.id, c.doc_id, c.section_path, c.body, d.title "
                "FROM chunks_fts f JOIN chunks c ON f.rowid = c.id "
                "JOIN docs d ON c.doc_id = d.id "
                "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_n * 3)
            ).fetchall()
        except Exception:
            pass

    # LIKE always runs as fallback/recall layer — catches substring matches
    # that tokenizer misses (e.g., compound words split across tokens)
    like_rows = db.execute(
        "SELECT c.id, c.doc_id, c.section_path, c.body, d.title "
        "FROM chunks c JOIN docs d ON c.doc_id = d.id "
        "WHERE c.body LIKE ? ORDER BY c.id LIMIT ?",
        (f"%{query}%", top_n * 3)
    ).fetchall()

    # Merge: FTS5 first (higher precision), then LIKE (recall)
    text_rows = list(fts_rows) + [r for r in like_rows if r[0] not in {row[0] for row in fts_rows}]

    for r in text_rows:
        cid = r[0]
        if cid not in seen:
            seen.add(cid)
            results.append({
                "chunk_id": cid, "doc_id": r[1], "section": r[2],
                "body": r[3][:body_max_len] if body_max_len else r[3], "doc_title": r[4], "source": "text"
            })

    # Vector results (if available)
    if query_vec and len(query_vec) > 0:
        has_vec = db.execute("SELECT value FROM _meta WHERE key = 'has_vectors'").fetchone()
        if has_vec and has_vec[0] == '1':
            vec_rows = db.execute(
                "SELECT c.id, c.doc_id, c.section_path, c.body, d.title, v.distance "
                "FROM chunks_vec v "
                "JOIN chunks c ON v.rowid = c.id "
                "JOIN docs d ON c.doc_id = d.id "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (json.dumps(query_vec), top_n * 2)
            ).fetchall()

            for r in vec_rows:
                cid = r[0]
                if cid not in seen:
                    seen.add(cid)
                    results.append({
                        "chunk_id": cid, "doc_id": r[1], "section": r[2],
                        "body": r[3][:body_max_len] if body_max_len else r[3], "doc_title": r[4], "source": "vector"
                    })

    # Rerank: re-score merged results by relevance (if reranker configured)
    if reranker and len(results) > 1:
        try:
            docs = [r["body"] for r in results]
            scored = await reranker.rerank(query, docs, min(top_n, len(docs)))
            # Reorder by reranker score
            reranked = [results[i] for i, _ in scored if i < len(results)]
            # Append any results the reranker didn't score
            scored_indices = {i for i, _ in scored}
            reranked += [r for j, r in enumerate(results) if j not in scored_indices]
            results = reranked
        except Exception:
            pass  # Rerank failure → keep original order

    return results[:top_n]
