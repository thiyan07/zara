# Memory Architecture (`core/memory.py`, `core/memory_policy.py`)

## Layers

- `MemoryStore`: in-memory hybrid retrieval (embedding cosine + keyword
  overlap), metadata (source/confidence/importance/created/updated/expires),
  secret refusal, supersede filtering.
- `PersistentMemoryStore`: same behavior backed by SQLite (`ZARA_MEMORY_DB`
  or `assistant.db`). Embeddings serialize as JSON locally. Production path:
  same rows in PostgreSQL + pgvector (`embedding vector(N)` + ivfflat index)
  — see `core/db.py` notes. No heavyweight vector DB required locally.
- `MemoryService`: the ONLY writer. Every candidate passes
  `classify_write` (see MEMORY_POLICY.md). Corrections via `correct()` keep
  the old record as provenance (`superseded_by`), never silently authoritative.

## Types

preferences | project | task | episodic | documents | device (+ system
task-history written automatically on mission completion, importance 0.4).

## Retrieval

Bounded top-k (default 3 into prompts), category filter, skips expired and
superseded items, scored hybrid. Retrieved memories enter prompts wrapped as
`<<UNTRUSTED DATA>>` — they inform, never authorize. Privacy recorded per
item (`metadata.privacy`); secrets can never be stored, hence never retrieved.
