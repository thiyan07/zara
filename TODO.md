# Zara Stage 4 — Todo

Forensics (done):
- [x] Repo at f299967, clean tree, 62/62 Python pass
- [x] No physical Android (adb empty), no LLM keys, 22GB free / 14GB RAM
- [x] opencode CLI present; Chrome + Playwright browsers cached; no ollama

Phase 1 — Real LLM path:
- [x] Stub OpenAI-compatible server fixture; real-HTTP round-trip test
- [x] Key-leak audit tests (header-only travel, no metadata/log leak)
- [x] Retry/auth-fail-fast/stream-malformed covered

Phase 2 — Android secure storage:
- [ ] `lib/secure_store.dart` (secure plugin + fallback abstraction)
- [ ] Claim flow stores key securely; never logs it
- [ ] Dart tests; hardware marked NOT VALIDATED

Phase 3 — WebSocket realtime:
- [x] Authenticated `/v1/stream` (dev token or device creds, events read-only)
- [x] Bounded per-connection queue, heartbeat, `?since=` replay, invalid-msg reject
- [x] Tests: auth, live event, replay, disconnect, no-exec-over-WS

Phase 4 — Wake word honesty:
- [ ] Availability reporting endpoint/contract; no fake detection
- [ ] Tests for states; docs mark NOT VALIDATED on hardware

Phase 5 — Auth hardening:
- [x] Job-result idempotency; pairing single-use verified; rotation API
- [x] Rate limiting middleware (pairing/auth stricter)
- [x] Tests: replay, revoke, rotation, rate-limit, invalid token

Phase 6 — Governor/queues:
- [ ] JobQueue per-device cap + overflow behavior; tests

Phase 7 — OpenCode tools (scoped coding specialist):
- [x] `core/opencode_tools.py`: inspect/read/search/test/diff + gated apply_patch
- [x] Workspace scoping, timeouts, audit; TEST J live on scratch repo

Phase 8 — Browser tools (Playwright, Chrome present):
- [x] `core/browser_tools.py`: open/extract/click-allowlisted/fill/screenshot/close
- [x] Untrusted-output wrapping; confirm+ for side effects; TEST K live local page

Phase 9 — DB/recovery + deploy + observability:
- [ ] WAL, file-backed audit (env), backup/restore, restart-restore tests
- [ ] Dockerfile/compose, .env.example, /v1/ready, DEPLOYMENT.md (local-validated only)

Phase 10 — Failure matrix + E2E A–L + reports:
- [ ] Missing failure tests (WS, replay, overflow, concurrency, restart)
- [ ] E2E runs, TEST_REPORT.md, STAGE4_STATUS.md, docs, commits, final report
