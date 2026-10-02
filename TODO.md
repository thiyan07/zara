# Zara Stage 5 — Todo (MCP + sandbox + real-world validation)

Baseline (done): 90/90 Python, 10/10 Flutter, analyze clean, HEAD 16d45f8,
20GB free, no device, no keys. Old builds cleaned (1 APK, newest backups).

- [x] Forensic baseline + cleanup old builds/backups
- [x] MCP core adapter (`core/mcp.py`: config/client/descriptor/transport)
- [x] MCP registry: namespaced Zara tools, schema bounds, risk mapping
- [x] Deterministic MCP test server fixture (stdio, incl. malicious tool)
- [x] MCP tests: discovery, execution, 7 injection cases, bounds
- [x] Sandbox abstraction (`core/sandbox.py`, app-level, honest limits)
- [x] OpenCode/browser routed through sandbox; re-test
- [x] Real-world validation: LLM keys? android? STT/TTS? wake? opencode CLI?
- [x] Security regression (19-attack matrix)
- [x] Performance spot measures (real numbers)
- [x] E2E A–S final checklist live
- [x] Docs: MCP/SANDBOX/REAL_WORLD/STAGE5_STATUS/TEST_REPORT, README/TODO
- [x] APK rebuild (only if Android code changes), cleanup, final commit+report
