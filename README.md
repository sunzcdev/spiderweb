# Spiderweb 🕸️

LLM-powered three-layer document graph engine.

**L1** — ingest documents → chunks → full-text + vector search  
**L2** — LLM auto-extracts entities + semantic relations → knowledge graph  
**L3** — record human insights → auto-connect back to L2 entities

Domain-agnostic. Reading is just one domain pack.

## Install

```bash
pip install spiderweb
```

## Quick Start

```bash
# 1. Initialize a domain pack
spiderweb init --target ~/my-reading-graph --template reading

# 2. Edit config and API keys
cd ~/my-reading-graph
cp .env.example .env
# Edit .env with your API keys
# Edit config.yaml to customize

# 3. Install skill for Claude Code
spiderweb install-skill --domain ~/my-reading-graph

# 4. Add MCP server to .mcp.json
```

In `.mcp.json`:

```json
{
  "mcpServers": {
    "spiderweb-reading": {
      "command": "spiderweb",
      "args": ["serve", "--domain", "~/my-reading-graph"]
    }
  }
}
```

## Architecture

```
documents ──ingest──► L1 Chunks (FTS + Vector)
                          │
                    graph_build (LLM extraction)
                          │
                          ▼
                    L2 Knowledge Graph (entities + relations)
                          │
                    insight_record
                          │
                          ▼
                    L3 Insights (human notes → auto-link to L2)
```

## MCP Tools

| Layer | Tool | Description |
|-------|------|-------------|
| L1 | `doc_ingest` | Import document (.md, .txt, .epub) |
| L1 | `search_chunks` | Search document paragraphs |
| L1 | `doc_get` | Get chunk content by ID |
| L2 | `graph_build` | Build knowledge graph from docs |
| L2 | `graph_navigate` | Single-step graph traversal |
| L2 | `search_entities` | Search entities by name |
| L2 | `entity_register` | Register entity manually |
| L2 | `relation_set` | Create/update relation |
| L2 | `relation_list` | List entity relations |
| L3 | `insight_record` | Record insight (auto-extracts entities) |
| L3 | `insight_list` | List recent insights |
| L3 | `search_insights` | Search insights |
| Meta | `graph_stats` | Graph statistics |

## Domain Pack Structure

```
domains/my-domain/
├── domain.yaml       # Entity types, relation types, validation schema
├── config.yaml       # Chunk size, LLM config, data path
├── .env.example      # API key template
├── prompts/          # LLM prompt templates
├── hooks.py          # Optional: pre/post processing hooks
└── SKILL.md          # LLM skill instructions
```

## Philosophy

- **MCP = atomic tools** — search, get, set, navigate one step
- **Skill = orchestration** — LLM decides when to call what, how to compose
- **Domain pack = self-contained** — one directory = one graph application
- **One MCP instance = one graph** — data isolation through separate processes
