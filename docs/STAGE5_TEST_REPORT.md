# Test Report (Stage 5 final)

- Python: **100 passed** (90 prior intact + 10 new: 7 MCP/sandbox/regression
  in stage-5 file terms — full suite `python -m pytest tests/ -q`).
- Flutter: **10 passed**, `flutter analyze` clean, debug APK rebuilt.
- Live E2E A–S (real agent + real Chrome + real opencode CLI where noted):
  A echo ok · B battery 80% · C android-kind protocol ok · D voice turn ok ·
  E wake gating ok · F offline deny + reconnect success · G degraded marking ·
  H malicious memory denied · I evil file read, rm denied, 0 rm jobs ·
  J approval → pytest passed · K MCP calc `{"sum": 42}` verified ·
  L malicious MCP wrapped, nothing executed · M MCP send_message held ·
  N `/tmp` escape failed-closed · O 19/19 regression contained ·
  P restart recall 0.72 · Q audit chain intact · R 429s after 30 enrolls ·
  S rotation kills old key (403), new key works.
- Measured: local tool 0.4 ms, recall 0.1 ms, MCP call <1 ms,
  browser goto 9 ms (see REAL_WORLD_VALIDATION.md).
- Secrets leaked: NO (key-leak audit, redaction tests, scrubbed envs).
- Disk: 20 GB free start and end (floor 10 GB never approached; only
  pip playwright wheel + pub secure-storage added this stage).
- Deps added: none this stage (playwright + secure-storage were Stage 4).
