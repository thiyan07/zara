# MCP (`core/mcp.py`, fixture `tests/fixtures/mcp_test_server.py`)

Zara is an MCP **client/consumer**. MCP is an interoperability adapter, not
a new core: imported tools become ordinary Zara tools named
`mcp.<server>.<tool>` and pass through validation → policy → governor →
router → execution → verification → audit like everything else.

## Rules

- Servers come ONLY from the operator allowlist (`MCPRegistry.add_server`
  with an explicit `MCPServerConfig`). The LLM cannot invent servers, URLs,
  commands, paths, or credentials. No discovery from model text.
- stdio servers spawn with argv (no shell), scrubbed env, `cwd=~`; only
  explicitly named env vars pass through, never `*_API_KEY`/`ASSISTANT_TOKEN`.
- HTTP servers must be allowlisted `http(s)` URLs.
- Schemas are validated before registration: object-type only, ≤32 KB,
  nesting ≤6, known types only, descriptions ≤2000 chars, instruction-like
  descriptions rejected, ≤50 tools/server, ≤16 servers.
- Risk mapping (ONE permission model, no MCP-specific one): MCP
  `annotations.readOnlyHint` → safe; `destructiveHint` → confirm (an
  operator must explicitly pin `high_risk` to arm it further);
  unknown → confirm (never auto-safe). Arguments ≤8 KB, results ≤64 KB,
  wrapped in `<<UNTRUSTED_MCP_RESULT>>`.
- Results are data: fake approvals, secret bait, destructive suggestions,
  tool-confusion, and nested fake tool-calls inside MCP output change
  nothing (7-way injection battery in `tests/test_stage5.py`).

## Status

IMPLEMENTED + INTEGRATION TESTED over real stdio JSON-RPC (fixture server:
echo/calc/read_fixture/send_message/dangerous_delete/malicious_result) and
live dispatch (K/L/M/N). Zara as MCP *server*: DEFERRED (no architectural
reason found).
