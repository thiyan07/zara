# Memory Write Policy (deterministic — the LLM only suggests)

`classify_write(text)` returns one decision; `MemoryService` enforces it.

- **AUTO_SAVE** — explicit `"remember..."` / `"don't forget"` / stated
  preference (`I prefer...`, `my favorite...`). Stored, importance 0.9/0.8.
- **CANDIDATE** — durable-looking facts (`my X is...`, `we use...`), long
  substantive statements. Held in `pending_candidates`, stored only after
  `confirm_candidate` (user or high-confidence flow in later stages).
- **SESSION_ONLY** — greetings, questions, short/task requests, empty text.
  Never persisted.
- **NEVER_STORE** — anything matching secret markers (`secret`, `password`,
  `api key`, `token`, `private key`, `credential`). Refused; `remember()`
  raises even on direct calls.

`"Remember X"` (explicit) vs ordinary conversation is distinguished by the
`EXPLICIT_REMEMBER` patterns. `"My API key is ..."` is refused, never stored,
never embedded. Corrections supersede with provenance instead of deleting
history. Retrieval never returns secrets because secrets never enter.
