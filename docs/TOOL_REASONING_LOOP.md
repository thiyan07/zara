# Tool Reasoning Loop (the non-negotiable chain)

```
USER ("What's my laptop battery?")
 -> session + write-policy + mission(planning)
 -> recall (top-3, untrusted-labeled) + budgeted context
 -> relevant tools only (battery tool offered; destructive hidden)
 -> LLM proposes {"type":"tool_call","tool":"system.battery",...}
 -> validate: known tool, schema-valid args (else reject + retry in budget)
 -> policy: safe -> allow (confirm -> approval hold, destructive -> hard deny)
 -> governor: battery/network/cost check (defer/reroute, never silent drop)
 -> router: online + capable + cheapest-home device (core never bypassed)
 -> device agent executes allowlisted implementation
 -> verification: output checked, verified flag set by CORE, never the LLM
 -> result (status trusted, content untrusted-labeled) back to LLM
 -> LLM writes natural response from the verified result only
 -> mission completed + task-history memory + audit entries + trace
USER receives: "Laptop at 74%, charging."
```

Failure anywhere reports truthfully downstream: tool failure, denial,
deferral, timeout, and provider errors each produce honest user-facing
messages; the LLM is never told an operation succeeded when it did not.
Approval pauses the mission (`waiting_for_permission`); resume continues the
same turn. Budgets (calls/time/repeat-guard) stop loops safely.

Security invariants (all tested): unknown tools rejected, bad args rejected,
prompt-injected tool output/memory cannot change policy, secrets never in
prompts, LLM cannot self-verify or command devices.
