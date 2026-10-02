# Test Report (Stage 4 final)

- Python: **90 passed** (62 Stages 1–3 intact + 28 Stage 4), stable repeats.
  `python -m pytest tests/ -q`
- Flutter: **10 passed**, `flutter analyze` clean, debug APK builds.
- Live E2E (local services, real Linux agent, scripted LLM stand-in):
  - A local echo dispatch: succeeded, verified
  - B remote `system.battery`: succeeded, 80%, verified
  - C android-kind enroll/claim/register/heartbeat: online, caps, battery 82%
  - D voice turn (mock STT/TTS): ok, state idle, mission tracked
  - E wake: phrase exact; battery 10% → paused-low-battery; 80% → running
  - F offline dispatch → honest deny + offline presence; reconnect → succeeded
  - G low-battery degraded marking + governor defer (unit + live marking)
  - H malicious memory → `rm` still hard-denied
  - I evil file actually read via agent; `rm -rf /` denied; 0 rm jobs in audit
  - J `code.test` scratch repo: approval hold → approve → passed True
  - K browser open+extract local page: succeeded, content untrusted-wrapped
  - L memory marker written → process restarted → recalled (score 0.72)
- Security attack tests: forged/revoked/expired/replayed credentials,
  pairing reuse, rotation, rate-limit 429, WS unauthenticated/revoked close,
  WS action-frame refused, deny-pattern (incl. quoted variant), injection at
  memory/tool-output/file/webpage levels, secret-in-output redaction,
  key-leak audit (header-only travel). All contained.
- Failure matrix: LLM timeout/malformed/unavailable, tool timeout, device
  offline/reconnect, WS disconnect, duplicate events, queue overflow,
  concurrent dispatch (10 threads), process restart, approval resume,
  cancellation, opencode/browser failure containment.
- Disk: 21–22 GB free throughout (≥10 GB floor). No large downloads except
  pip `playwright` wheel (browsers already cached) and pub
  `flutter_secure_storage`.
