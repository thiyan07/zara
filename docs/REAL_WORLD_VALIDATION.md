# Real-World Validation (what was actually exercised)

## PHYSICALLY VALIDATED

Nothing requiring external hardware, hosted models, or cloud. (This file
exists to prevent category collapse.)

## INTEGRATION TESTED (real processes/protocols, local)

- LLM provider over real HTTP (stub OpenAI server): structured, retry,
  streaming, auth-fail-fast, key-leak audit.
- Linux agent over real HTTP+subprocess: dispatch/exec/approve, battery,
  filesystem, offline→deny→reconnect.
- MCP over real stdio JSON-RPC + live dispatch (safe/approval/malicious).
- OpenCode: `code.test`/`code.diff` live; `code.session` REAL CLI run
  (`opencode run`, model `muse-spark-1.3-contributor-free` via user config):
  temp repo, `double(x)` added, diff confined to workspace, function
  verified (`42`), no secrets. No `--auto` flag used.
- Browser over real Chrome 150: open/extract/click/fill guards, injection
  wrapping, failure containment, thread-safe worker.
- Restart persistence: memory + missions + audit across process restarts.
- WebSocket: auth, live fan-out, replay, read-only enforcement.

## BLOCKED (exact next manual step)

- REAL_HOSTED_LLM_VALIDATION: VALIDATED 2026-10-02 (openai/gpt-oss-20b on a
  free endpoint via `LLM_*` env: structured reply 16 s, full tool-use turn
  text -> system.battery -> real agent (80%) -> verified NL reply; key-leak
  audit of audit-DB/memory-DB/logs clean). Notes: the first configured
  model was end-of-life (truthful 410, correctly classified); bare
  `{"text": ...}` outputs are accepted as replies only, never as actions;
  `.env` stays gitignored and unread by git.
- PHYSICAL_ANDROID_VALIDATION: `adb devices` empty. Next: connect a phone
  with USB debugging, `adb devices` shows it, install
  `android/build/.../app-debug.apk`, pair via `/v1/agent/enroll`.
- Real STT/TTS/wake audio: no engine installed (no espeak/vosk/whisper
  models; no mic harness). Next: install a small STT (e.g. vosk model,
  ~50 MB — check disk first) and wire `STTProvider`.
- Real `opencode run` beyond the one validation: needs no extra step
  (works), but each task needs explicit user approval (HIGH_RISK).
- Docker image build + Oracle deploy: no docker daemon, no cloud account.
  Next: machine with docker → `docker compose up -d --build` → verify
  `/v1/ready`; Oracle steps in `docs/DEPLOYMENT.md`.

## Performance (measured, not invented)

Local tool 0.4 ms · memory write 0.3 ms · recall 0.1 ms · route 0.1 ms ·
exec submit 0.4 ms · MCP connect 29 ms (one-time spawn) · MCP list 1 ms ·
MCP call <1 ms · browser goto(blank) 9 ms.
