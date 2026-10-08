# RUNG 3 — Semantic Matching Specification

**Status: SPECIFICATION ONLY. No code changed, no device work.**
Evidence conventions (per `RUNG1_FINAL_REPORT.md`): **DEVICE** = measured on the ZaraLab emulator (API 36 userdebug) · **CORE-UNIT** = pytest, no device · **DOC** = official Android/AOSP documentation. Anything else is marked **NOT YET MEASURED**.

---

## 1. Matcher Priority Order (Deterministic, No Exceptions)

Given a `UiSnapshot` (fresh, ≤ 120s old) and a `TargetSelector`, resolution proceeds in **strict priority order**. The first priority level that yields **exactly one match** wins. Zero matches → continue to next priority. Two or more matches at the winning level → **AMBIGUOUS** (stop, fail).

| Priority | Selector Field | Match Logic | Notes |
|----------|----------------|-------------|-------|
| **1** | `resource_id` | Exact string equality, package-scoped | `com.app:id/btn_submit` matches only that id in `com.app` |
| **2** | `content_desc` | Exact string equality | Accessibility label; case-sensitive |
| **3** | `text` (exact) | Exact string equality | **Redacted nodes (password=true) are invisible — never match** |
| **4** | `text` (normalized) | Normalized equality (see §5) | NFC + lowercase + collapse whitespace |
| **5** | `role` + `tree_order` | Role match + ordinal index in DFS order | `role="button", index=2` → 3rd button in DFS pre-order |

### Selector Schema (input to `gui.element.find` / `gui.tap.target`)

```json
{
  "resource_id": "string | null",
  "content_desc": "string | null",
  "text": "string | null",
  "text_normalized": "boolean (default false — use priority 4)",
  "role": "string | null (one: button, checkbox, switch, edittext, image, text, other)",
  "index": "integer ≥ 0 | null (used only at priority 5)"
}
```

**Exactly one non-null field must be provided** (except `text_normalized`/`index` which are modifiers). Multiple non-null fields → **INVALID_QUERY**.

---

## 2. Coordinate Confirmation (8 dp, Never Blind)

- **Coordinates are derived ONLY from a matched node's `bounds.center`** in a fresh snapshot (≤ `max_age_s`, default 120s).
- **Tolerance**: Tap is confirmed if the injected gesture lands within **8 dp** of the node's center at the moment of execution.
- **No free-form (x, y) input** — Core/LLM/Prose never supplies coordinates. Any raw coordinate in a selector → **INVALID_QUERY** pre-handler.
- **No OCR-derived coordinates** — Threat model established in `PRIVILEGED_SECURITY_MODEL.md:178-188`.
- **Re-verification**: After tap, a fresh inspect (`≤ max_age_s`) confirms the target node still exists at expected bounds (±8 dp). Drift beyond tolerance → `VERIFICATION_FAILED`.

---

## 3. Ambiguity Handling

| Situation | Result | Job Output |
|-----------|--------|------------|
| 0 matches at all 5 priorities | **NO_MATCH** | `failed { reason: "no-match", selector, snapshot_id }` |
| 1 match at priority P, 0 at higher | **SUCCESS** | `applied: true, node_id, verify_state` |
| ≥2 matches at priority P (winning level) | **AMBIGUOUS** | `failed { reason: "ambiguous", match_count, candidates[≤5], snapshot_id }` |

### Ambiguity Candidates (max 5 returned)

```json
{
  "reason": "ambiguous",
  "match_count": 7,
  "candidates": [
    { "node_id": "n3", "resource_id": "com.app:id/item", "text": "Item 1", "bounds": {...} },
    { "node_id": "n7", "resource_id": "com.app:id/item", "text": "Item 2", "bounds": {...} },
    { "node_id": "n11", "resource_id": "com.app:id/item", "text": "Item 3", "bounds": {...} },
    { "node_id": "n15", "resource_id": "com.app:id/item", "text": "Item 4", "bounds": {...} },
    { "node_id": "n19", "resource_id": "com.app:id/item", "text": "Item 5", "bounds": {...} }
  ],
  "snapshot_id": "uuid..."
}
```

- **No indexes into hidden state** — Candidates are fully described (node_id, selector fields, bounds). Core/Human can re-dispatch with a narrower selector (e.g., add `text` or `index`).
- **Retrying the same ambiguous selector is a DENY**, not a defer (per RUNG2 §5).

---

## 4. Error Codes (Exhaustive)

| Code | HTTP-ish | When | Recovery |
|------|----------|------|----------|
| `NO_MATCH` | 404 | Zero matches across all priorities | Narrow selector; re-inspect; human |
| `AMBIGUOUS` | 409 | ≥2 matches at winning priority | Add discriminator (text, index); human |
| `STALE_SNAPSHOT` | 410 | `snapshot_id` mismatch or `now - timestamp_ms > 120000` | Fresh `gui.screen.inspect` → retry |
| `INVALID_QUERY` | 400 | Multiple selector fields, free-form coords, unknown role, index<0 | Fix selector schema |
| `UNEXPECTED_PACKAGE` | 403 | Foreground package ≠ approval-bound package | Re-approve for new package; human |
| `VERIFICATION_FAILED` | 422 | Post-action re-inspect predicate false | Stuck handling (RUNG2 §7) |
| `SERVICE_UNAVAILABLE` | 503 | AccessibilityService unbound, inspect failed | Wait for probe `ready:true`; re-register |

**All errors complete the job terminally (`failed`)** — never `expired` by silence. Audit record always written.

---

## 5. Expected Package Guard (Exact Match on Foreground)

- Every `gui.*` action carries an **approval-bound `package_name`** (from the capability descriptor's `requires` / approval card).
- **Before selector resolution**, device checks: `snapshot.package_name == approval_package`.
- **Mismatch → `UNEXPECTED_PACKAGE`** (structured refusal, pre-handler).
- **No prefix/wildcard/substring matching** — Exact string equality only.
- **Secure/FLAG_SECURE windows** → `snapshot.package_name == ""` → always mismatch → `UNEXPECTED_PACKAGE` (by design; no UI automation on secure windows).

### Dual Allowlist (RUNG1 §5 / RUNG2 §5 pattern)

| Layer | Allowlist | Location |
|-------|-----------|----------|
| Core (Python) | `GUI_LAB_TARGETS = frozenset({"dev.zara.zara_android", "dev.zara.lab.privtest"})` | `core/device_tools.py` |
| Device (Kotlin) | `ALLOWED_PACKAGES = setOf("dev.zara.zara_android", "dev.zara.lab.privtest")` | `ZaraA11yService.kt` |

Both must contain the target package. Any package outside → deny at respective layer.

---

## 6. Text Normalization (Priority 4 Only)

Applied **only** when `text_normalized: true` in selector (priority 4). Never applied to priority 3 (exact).

```
normalize(s: string): string
    1. Unicode NFC normalization (NFD → compose)
    2. Lowercase (Locale.ROOT, not device locale)
    3. Collapse whitespace: \\s+ → single space (U+0020)
    4. Trim leading/trailing whitespace
    5. NO translation, NO transliteration, NO paraphrase, NO synonym expansion
    6. NO emoji normalization (emoji stay as-is post-NFC)
```

**Examples:**

| Original | Normalized |
|----------|------------|
| `"  Submit  "` | `"submit"` |
| `"Log\u00A0In"` (NBSP) | `"log in"` |
| `"ÉLITE"` (NFC) | `"élite"` |
| `"Password\u200B"` (ZWSP) | `"password"` |
| `"🔐 Login"` | `"🔐 login"` |

**Redacted nodes** (`password: true`) are **excluded before normalization** — they never participate in matching at any priority.

---

## 7. Role Taxonomy (Priority 5)

| Role Value | Maps To (class_name heuristics) |
|------------|----------------------------------|
| `button` | `Button`, `ImageButton`, `MaterialButton`, `FloatingActionButton`, `*Button` |
| `checkbox` | `CheckBox`, `CheckableImageButton`, `CompoundButton` |
| `switch` | `Switch`, `SwitchCompat`, `MaterialSwitch` |
| `edittext` | `EditText`, `TextInputEditText`, `AutoCompleteTextView`, `MultiAutoCompleteTextView` |
| `image` | `ImageView`, `ImageButton`, `PhotoView`, `*ImageView` |
| `text` | `TextView`, `AppCompatTextView`, `MaterialTextView`, `*TextView` |
| `other` | Everything else (ViewGroup, RecyclerView, custom views) |

**Role is derived device-side** from `class_name` at snapshot time. Selector `role` must match exactly one of the 7 values above.

---

## 8. Interaction with Action→Verify Loop (RUNG2 §6)

```
gui.tap / gui.element.find receives TargetSelector
         |
         v
Fresh inspect (or supplied snapshot_id) → UiSnapshot
         |
         v
Package guard (§5) → UNEXPECTED_PACKAGE or continue
         |
         v
Matcher (§1) → single node_id OR NO_MATCH / AMBIGUOUS
         |
         v
Coordinate confirmation (§2) → tap at center ±8 dp
         |
         v
Re-inspect (fresh, ≤ max_age_s) → verify predicate
         |
         v
PASS → done (verify_state carried)
FAIL → retry (idempotent only, ≤3) → stuck handling (§7 RUNG2)
```

---

## 9. Cross-References

- `RUNG2_UICONTROL.md` §5 (targeting priority), §6 (action→verify), §7 (stuck detection)
- `RUNG3_UI_SNAPSHOT.md` (schema consumed by matcher)
- `RUNG3_FORENSIC_REPORT.md` (template for rung completion)

---

*Spec version: 1.0 | Generated: 2026-10-07*