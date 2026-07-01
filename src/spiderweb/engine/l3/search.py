"""L3 search — insight search (FTS5 + LIKE + vector)."""
import json
from ..providers import create_tokenizer_provider, create_embedding_provider


async def search_insights(db, query: str, config, top_n: int = 5) -> list[dict]:
    """Search insights by title and content. FTS5 (jieba) + LIKE + vector → dedup → top_n."""
    results = []
    seen = set()

    # FTS5 (jieba-tokenized, word-level precision)
    tokenizer = create_tokenizer_provider(config.tokenizer)
    if tokenizer:
        tokenized = tokenizer.tokenize(query)
        tokens = tokenized.split()
        fts_query = " ".join(f"{t}*" for t in tokens) if tokens else query
    else:
        fts_query = query

    try:
        fts_rows = db.execute(
            "SELECT i.id, i.slug, i.title, i.content, i.created_at FROM insights i "
            "JOIN insights_fts f ON i.rowid = f.rowid "
            "WHERE insights_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, top_n * 2)
        ).fetchall()
        for r in fts_rows:
            if r[0] not in seen:
                seen.add(r[0])
                results.append({
                    "id": r[0], "slug": r[1], "title": r[2],
                    "content": r[3][:300], "created_at": r[4], "source": "fts",
                })
    except Exception:
        pass

    # LIKE fallback (substring recall)
    like_rows = db.execute(
        "SELECT id, slug, title, content, created_at FROM insights "
        "WHERE title LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", f"%{query}%", top_n * 2)
    ).fetchall()
    for r in like_rows:
        if r[0] not in seen:
            seen.add(r[0])
            results.append({
                "id": r[0], "slug": r[1], "title": r[2],
                "content": r[3][:300], "created_at": r[4], "source": "like",
            })

    # Vector search (semantic)
    emb_provider = create_embedding_provider(config.embedding)
    if emb_provider:
        has_vec = db.execute(
            "SELECT value FROM _meta WHERE key = 'has_vectors_insights_vec'"
        ).fetchone()
        if has_vec and has_vec[0] == '1':
            try:
                vecs = await emb_provider.embed([query])
                query_vec = vecs[0]
                vec_rows = db.execute(
                    "SELECT i.id, i.slug, i.title, i.content, i.created_at, v.distance "
                    "FROM insights_vec v "
                    "JOIN insights i ON v.rowid = i.id "
                    "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                    (json.dumps(query_vec), top_n * 2)
                ).fetchall()
                for r in vec_rows:
                    if r[0] not in seen:
                        seen.add(r[0])
                        results.append({
                            "id": r[0], "slug": r[1], "title": r[2],
                            "content": r[3][:300], "created_at": r[4], "source": "vector",
                        })
            except Exception:
                pass

    return results[:top_n]
