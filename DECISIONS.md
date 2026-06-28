# Spiderweb — Design Decisions

## What
LLM-powered three-layer document graph engine. Domain-agnostic.
- L1: Source documents (ingest → chunks → FTS5 + vector)
- L2: Knowledge graph (entities + semantic relations, auto-extracted by LLM)
- L3: Insights (human notes, LLM reverse-extracts entities back to L2)

Reading is one domain pack; users create their own.

## MCP/Skill boundary
- MCP server: atomic tools (search, get, set, navigate one step)
- Skill (LLM): orchestration — when to call what, how to compose

## Tech stack
- Python 3.12, uv, mcp SDK
- SQLite + sqlite-vec (hard dep)
- Embedding provider: configurable per graph, cannot change after creation
- Other deps optional: reranker, jieba tokenizer, HDBSCAN, PaddleOCR

## Domain pack
```
domains/reading/
├── domain.yaml       # entity types, relation types, valid_triplets, doc meta schema
├── config.yaml       # chunk size, LLM config, data path, extraction mode
├── .env.example      # API key template
├── prompts/          # extract_entities.md, extract_relations.md, record_insight.md
├── hooks.py          # optional: pre_ingest[], post_extract[], validators[] — chainable
└── SKILL.md          # domain-specific LLM instructions
```

## MCP tools (MVP)
L1: doc_ingest, search_chunks, search_entities, search_insights, doc_get
L2: graph_build, graph_navigate, entity_get, entity_register, relation_set, relation_list
L3: insight_record, insight_list
Meta: graph_stats

## Key design choices
- One MCP instance = one engine + one domain pack + one graph database
- graph_navigate: single-step, returns anchored vs exploration edges (60% signal, 40% freedom)
- graph_build: full or sample mode, default sample
- Entity extraction: single-pass (entities + relations in one LLM call)
- Providers: abstract base classes, configured in config.yaml
- Doc formats: .md + .txt + .epub (pandoc)
- No migration: old data stays as-is, new data follows current schema
- Entity summaries (entity_concepts) kept as independent layer between L1 and L3
- SKILL.md ships with domain pack; install via `spiderweb install-skill`
- MCP startup: `--domain` flag or `SPIDERWEB_DOMAIN` env var

## MVP excludes
- interest_list (HDBSCAN), reranker, PDF OCR — later plugins
