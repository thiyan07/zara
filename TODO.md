# Zara Stage 5 — Todo (MCP + sandbox + real-world validation)

Baseline (done): 90/90 Python, 10/10 Flutter, analyze clean, HEAD 16d45f8,
20GB free, no device, no keys. Old builds cleaned (1 APK, newest backups).

- [x] Forensic baseline + cleanup old builds/backups
- [x] MCP core adapter (`core/mcp.py`: config/client/descriptor/transport)
- [x] MCP registry: namespaced Zara tools, schema bounds, risk mapping
- [x] Deterministic MCP test server fixture (stdio, incl. malicious tool)
- [x] MCP tests: discovery, execution, 7 injection cases, bounds
- [ ] Sandbox abstraction (`core/sandbox.py`, app-level, honest limits)
- [ ] OpenCode/browser routed through sandbox; re-test
- [ ] Real-world validation: LLM keys? android? STT/TTS? wake? opencode CLI?
- [ ] Security regression (19-attack matrix)
- [ ] Performance spot measures (real numbers)
- [ ] E2E A–S final checklist live
- [ ] Docs: MCP/SANDBOX/REAL_WORLD/STAGE5_STATUS/TEST_REPORT, README/TODO
- [ ] APK rebuild (only if Android code changes), cleanup, final commit+report
