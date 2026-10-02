# Stage 5 Status (MCP + sandbox + real-world validation)

Same Zara, same chain. Stage 5 adds interop + isolation, no new authority.

## Ledger

| Item | Status |
|---|---|
| MCP client adapter (stdio+http) | IMPLEMENTED + INTEGRATION TESTED |
| MCP registry/schema bounds/risk map | IMPLEMENTED + UNIT TESTED |
| MCP test server fixture | IMPLEMENTED + INTEGRATION TESTED |
| MCP 7-way injection battery | INTEGRATION TESTED (all contained) |
| MCP live E2E (K safe / L malicious / M approval-gated) | INTEGRATION TESTED |
| Sandbox abstraction | IMPLEMENTED + UNIT TESTED |
| OpenCode via sandbox | INTEGRATION TESTED |
| Real `opencode run` validation | INTEGRATION TESTED (one real task, confined) |
| Browser via worker + guards | INTEGRATION TESTED (real Chrome) |
| Rate limits / rotation / idempotency | INTEGRATION TESTED |
| Backup/restore/restart | INTEGRATION TESTED |
| WebSocket realtime | INTEGRATION TESTED |
| Android secure storage | UNIT TESTED |
| 19-attack regression | INTEGRATION TESTED (19/19 contained) |
| E2E A–S | INTEGRATION TESTED (live where possible) |
| Real hosted LLM | BLOCKED (no credential) |
| Physical Android/audio/wake | BLOCKED (no hardware/engine) |
| Docker build / Oracle deploy | DEFERRED (no daemon/account) |
| Zara as MCP server | DEFERRED (no reason found) |

## Bugs found & fixed this stage

1. MCPClient non-reentrant lock deadlock (RLock).
2. Nondeterministic `hash()` embeddings → stable md5 (fixes flaky recall).
3. Playwright thread-affinity crash → dedicated browser worker thread.
4. `pytest` not on restricted PATH → fixed-dir binary resolution.
5. `opencode run` has no `--cwd` in CLI 1.18 → cwd carries workspace.
6. Shared browser left on error document → session reset on failed nav.
7. Whitespace-collapsed edits broke two files mid-stage; repaired + compile-gated.
