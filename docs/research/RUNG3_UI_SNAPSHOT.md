# RUNG 3 — UiSnapshot Schema & Traversal Specification

**Status: SPECIFICATION ONLY. No code changed, no device work.**
Evidence conventions (per `RUNG1_FINAL_REPORT.md`): **DEVICE** = measured on the ZaraLab emulator (API 36 userdebug) · **CORE-UNIT** = pytest, no device · **DOC** = official Android/AOSP documentation. Anything else is marked **NOT YET MEASURED**.

---

## 1. UiSnapshot Schema (Complete)

```json
{
  "snapshot_id": "string (UUIDv7, monotonically increasing per device session)",
  "timestamp_ms": "integer (epoch ms, monotonic clock)",
  "package_name": "string (foreground package at snapshot time)",
  "activity_name": "string (foreground activity, may be empty on secure windows)",
  "screen_bounds": { "left": 0, "top": 0, "right": 1080, "bottom": 2340 },
  "rotation": 0,
  "nodes": [
    {
      "node_id": "string (stable per-snapshot: "n0", "n1" …)",
      "resource_id": "string | null (package-scoped, e.g. "com.app:id/btn")",
      "text": "string | null (redacted for password/secure nodes → empty + redacted:true)",
      "content_desc": "string | null",
      "class_name": "string (View fully-qualified class)",
      "bounds": { "left": "int", "top": "int", "right": "int", "bottom": "int" },
      "center": { "x": "int", "y": "int" },
      "clickable": "boolean",
      "long_clickable": "boolean",
      "scrollable": "boolean",
      "editable": "boolean",
      "focused": "boolean",
      "selected": "boolean",
      "checked": "boolean",
      "checkable": "boolean",
      "enabled": "boolean",
      "visible_to_user": "boolean",
      "password": "boolean (true if isPassword or TYPE_TEXT_VARIATION_PASSWORD)",
      "autofill_hints": "string[] | null",
      "redacted": "boolean (true when text/content_desc suppressed for security)",
      "children": "string[] (child node_ids, empty for leaves)"
    }
  ],
  "truncated": "boolean (true when bounds exceeded)",
  "truncation_metadata": {
    "nodes_before_truncation": "integer",
    "nodes_after_truncation": "integer (≤ 50)",
    "nodes_visited": "integer (≤ 500)",
    "max_depth_reached": "integer (≤ 20)",
    "text_truncations": "integer",
    "content_desc_truncations": "integer",
    "class_name_truncations": "integer",
    "autofill_hints_truncated": "boolean"
  },
  "screenshot_available": "boolean (seam — true when MediaProjection/AccessibilityService.takeScreenshot path is wired; false in Rung 3)"
}
```

### Field Definitions & Invariants

| Field | Required | Type | Invariant |
|-------|----------|------|-----------|
| `snapshot_id` | ✅ | UUIDv7 | Unique per inspect call; Core uses for staleness check (120s) |
| `timestamp_ms` | ✅ | int64 | Monotonic; device-side clock |
| `package_name` | ✅ | string | Must match foreground at snapshot time; empty on secure/FLAG_SECURE windows |
| `activity_name` | ❌ | string | Best-effort; empty if unresolved |
| `screen_bounds` | ✅ | Rect | Full display bounds (not app window) |
| `rotation` | ✅ | 0/90/180/270 | `Display.getRotation()` |
| `nodes[]` | ✅ | array | **Max 50 elements** after truncation; DFS order |
| `truncated` | ✅ | boolean | True iff any bound exceeded |
| `truncation_metadata` | ✅ | object | Always present; counts for audit |
| `screenshot_available` | ✅ | boolean | **Seam for future `gui.screen.capture`**; false until wired |

---

## 2. Bounds Table

| Bound | Value | Enforcement Point | Source |
|-------|-------|-------------------|--------|
| Max nodes per snapshot | **50** | Device-side truncation after DFS | Spec |
| Max `text` length per node | **200 chars** | Truncate + `text_truncations++` | Spec |
| Max `content_desc` length per node | **128 chars** | Truncate + `content_desc_truncations++` | Spec |
| Max `class_name` length per node | **120 chars** | Truncate + `class_name_truncations++` | Spec |
| Max `autofill_hints` combined | **120 chars** | Join with `,`; truncate + `autofill_hints_truncated=true` | Spec |
| Max tree depth | **20** | Stop descent; count toward `max_depth_reached` | Spec |
| Max nodes visited during DFS | **500** | Abort traversal; `nodes_visited` recorded | Spec |
| Snapshot freshness (staleness) | **120 s** | Core rejects if `now - timestamp_ms > 120000` | RUNG2 §4 |
| Node ID format | `n{index}` | Sequential 0..N-1 in DFS order; stable per snapshot | Spec |

**Truncation metadata format** (always emitted):

```json
{
  "nodes_before_truncation": 237,
  "nodes_after_truncation": 50,
  "nodes_visited": 500,
  "max_depth_reached": 20,
  "text_truncations": 12,
  "content_desc_truncations": 3,
  "class_name_truncations": 1,
  "autofill_hints_truncated": true
}
```

---

## 3. Traversal Algorithm (DFS, Cycle Protection, Node Recycling)

```
function buildSnapshot(root: AccessibilityNodeInfo): UiSnapshot
    visited = new Set<Int>()           // System hash codes for cycle detection
    nodePool = new ArrayList<Node>()   // Pre-allocate 50 slots
    nodeMap = new HashMap<Int, String>() // sysHash → node_id
    truncMeta = new TruncationMeta()

    function dfs(node: AccessibilityNodeInfo, depth: int): String?
        if node == null or depth > 20 or truncMeta.nodesVisited >= 500
            return null

        sysHash = System.identityHashCode(node)
        if visited.contains(sysHash)
            return null                 // Cycle protection
        visited.add(sysHash)

        truncMeta.nodesVisited++

        // Build node record (apply char caps)
        nodeId = "n" + nodePool.size
        record = extractNode(node, nodeId, truncMeta)
        nodePool.add(record)
        nodeMap.put(sysHash, nodeId)

        // Recurse children (DFS, left-to-right)
        childIds = []
        for i in 0..node.childCount-1
            child = node.getChild(i)
            childId = dfs(child, depth + 1)
            if childId != null
                childIds.add(childId)
        record.children = childIds

        // Hard cap: stop adding once 50 nodes collected
        if nodePool.size >= 50
            return nodeId               // Still return this node's id to parent

        return nodeId

    rootId = dfs(root, 0)
    truncated = (truncMeta.nodesVisited > 50 or truncMeta.maxDepthReached > 20
                 or truncMeta.textTruncations > 0
                 or truncMeta.contentDescTruncations > 0
                 or truncMeta.classNameTruncations > 0
                 or truncMeta.autofillHintsTruncated)

    return UiSnapshot(
        snapshotId = UUIDv7(),
        timestampMs = SystemClock.uptimeMillis(),
        packageName = root.packageName ?: "",
        activityName = resolveActivity(root),
        screenBounds = getScreenBounds(),
        rotation = getRotation(),
        nodes = nodePool,
        truncated = truncated,
        truncationMetadata = truncMeta,
        screenshotAvailable = false     // Seam — wired in later rung
    )
```

### Key Properties

| Property | Guarantee |
|----------|-----------|
| **Deterministic order** | DFS pre-order, children left-to-right (ViewGroup iteration order) |
| **Cycle protection** | `System.identityHashCode` + `visited` set; max 500 visits |
| **Node recycling** | Pre-allocated `ArrayList(50)`; no new allocations after cap |
| **Stable node_ids** | `n0..n49` assigned in traversal order; parent `children` arrays reference by id |
| **No lazy expansion** | Entire snapshot built atomically; no follow-up IPC for children |
| **Secure window handling** | `FLAG_SECURE` windows → `package_name=""`, `nodes=[]`, `truncated=false` |

---

## 4. Sensitive Detection Rules (Device-Side Redaction)

A node is marked `password: true` and its `text`/`content_desc` replaced with `""` + `redacted: true` iff **any** of:

| Condition | Check |
|-----------|-------|
| `isPassword` | `node.isPassword == true` (API 18+) |
| Input type variation | `(node.inputType & InputType.TYPE_TEXT_VARIATION_PASSWORD) != 0` |
| Autofill hints | `node.autofillHints` contains any of: `["password", "currentPassword", "newPassword", "oneTimeCode", "pin", "securityCode"]` (case-insensitive) |
| Class name heuristic | `className` ends with `PasswordEditText`, `PinEntryView`, `TextInputLayout` with password mode (best-effort, defensive) |

**Redaction is irreversible** — Core/audit/LLM never receive original text. The `redacted: true` flag travels in the snapshot for audit traceability.

---

## 5. screenshot_available Seam

```json
"screenshot_available": false
```

- **Rung 3**: Always `false`. Documents the future seam for `gui.screen.capture`.
- **Future rung**: When `AccessibilityService.takeScreenshot()` (API 30+) or MediaProjection path is approved + wired, this flips to `true` and `gui.screen.capture` descriptor becomes available.
- **No pixels in snapshot**: The field is a boolean capability flag only; actual screenshot bytes travel via separate handle/reference (never inline in job envelope per RUNG2 §3).

---

## 6. Error / Unavailable Shapes

When `gui.screen.inspect` cannot produce a snapshot:

```json
// No active window (secure, keyguard, or no foreground app)
{ "supported": true, "ready": false, "reason": "no active window", "nodes": [] }

// Service not enabled / unbound
{ "supported": true, "ready": false, "reason": "accessibility service not enabled", "nodes": [] }

// Service enabled but inspect failed (crash, ANR, IPC error)
{ "supported": false, "ready": false, "reason": "inspect failed: <error>", "nodes": [] }
```

These shapes are **not** `UiSnapshot` — they are the `a11yInspect` raw result that Core wraps into the job result envelope.

---

## 7. Cross-References

- `RUNG2_UICONTROL.md` §4 (snapshot bounds), §5 (targeting), §6 (action→verify loop)
- `RUNG2_RELIABILITY_ADDENDUM.md` (staleness, verdicts)
- `RUNG3_SEMANTIC_MATCHING.md` (how selectors consume this schema)
- `RUNG3_FORENSIC_REPORT.md` (template for rung completion)

---

*Spec version: 1.0 | Generated: 2026-10-07*