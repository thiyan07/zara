"""UI control layer — snapshots, element matching, privilege classes,
stuck detection, registration guard, and action verification.

Pure Python (stdlib + pydantic only). Deterministic: no LLM, no I/O.
Snapshots are UNTRUSTED input: bounded, strictly validated, secret-scrubbed.

Style follows core/tools.py (envelope bounds) and core/capabilities.py
(strict descriptors, extra="forbid", audited rejections).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# Bounds (mirror tools.py envelope discipline + task limits)
# --------------------------------------------------------------------------

MAX_ELEMENTS = 50
MAX_DEPTH = 10
MAX_ROLE_LEN = 40
MAX_RESOURCE_ID_LEN = 128
MAX_CONTENT_DESC_LEN = 120
MAX_TEXT_LEN = 120
MAX_TIMEOUT_S = 60
MAX_RETRY = 2
MAX_STEPS = 8
COORD_TOLERANCE_DP = 8.0
MAX_PROBE_PING_MS = 2000.0

REDACTED = "[redacted]"

# Values mentioning these patterns are secrets (or secret-adjacent) and are
# scrubbed to "[redacted]" at parse time. Matches against the *value*, so a
# resource_id like "...:id/password" is redacted too — fail closed.
_SECRET_RE = re.compile(
    r"password|passwd|token|api[_-]?key|apikey|secret|credential|private[_-]?key",
    re.IGNORECASE,
)

DIALOG_PACKAGES = frozenset({
    "com.android.permissioncontroller",
    "com.android.systemui",
})

# Target package considered "gone" (process died) rather than switched.
CRASH_PACKAGES = frozenset({"", "android"})

_FORBID = ConfigDict(extra="forbid")


def _scrub(value: str) -> str:
    if isinstance(value, str) and _SECRET_RE.search(value):
        return REDACTED
    return value


def _clip(value: str, limit: int) -> str:
    value = _scrub(value)
    if len(value) > limit:
        return value[:limit]
    return value


def normalize_text(value: str) -> str:
    """Lowercase + collapse whitespace + Unicode NFC normalization (matcher canonical form).
    Strips bidi control characters and other formatting marks."""
    if not isinstance(value, str):
        value = str(value)
    # Unicode NFC normalization
    value = unicodedata.normalize("NFC", value)
    # Replace null bytes and BOM with space (not empty) so they act as word separators
    value = value.replace("\x00", " ").replace("\ufeff", " ")
    # Remove bidi control characters: U+200E, U+200F, U+202A..U+202E, U+2066..U+2069
    bidi_chars = "".join(chr(c) for c in range(0x200E, 0x200F+1))  # LRM, RLM
    bidi_chars += "".join(chr(c) for c in range(0x202A, 0x202E+1))  # LRE, RLE, PDF, LRO, RLO
    bidi_chars += "".join(chr(c) for c in range(0x2066, 0x2069+1))  # LRI, RLI, FSI, PDI
    for ch in bidi_chars:
        value = value.replace(ch, " ")
    # Collapse whitespace and lowercase
    return " ".join(value.lower().split())


def _snapshot_hash(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def check_expected_package(snapshot: UiSnapshot, expected: str | None) -> Literal["MATCH", "UNEXPECTED_PACKAGE"]:
    """Guard: verify snapshot package matches expected package regex.
    Returns "MATCH" if expected is None/empty or matches, else "UNEXPECTED_PACKAGE"."""
    if not expected:
        return "MATCH"
    try:
        pattern = re.compile(expected)
        pkg = snapshot.package_name or ""
        if pattern.fullmatch(pkg):
            return "MATCH"
    except re.error:
        # Invalid regex -> treat as mismatch for safety
        return "UNEXPECTED_PACKAGE"
    return "UNEXPECTED_PACKAGE"


def is_stale(snapshot_id: str, current_snapshot_id: str) -> bool:
    """Check if snapshot_id differs from current (stale snapshot)."""
    return bool(snapshot_id and current_snapshot_id and snapshot_id != current_snapshot_id)


# --------------------------------------------------------------------------
# 1. Snapshot models
# --------------------------------------------------------------------------

class UiElement(BaseModel):
    """One UI node. Malformed nodes raise here and are *skipped* by the
    snapshot pre-validator — parsing a snapshot never raises for bad nodes."""

    model_config = _FORBID

    id: str
    role: str = ""
    resource_id: str = ""
    content_desc: str = ""
    text: str = ""
    bounds: list[int] = Field(default_factory=lambda: [0, 0, 0, 0])
    enabled: bool = True
    selected: bool = False
    checked: bool = False
    scrollable: bool = False
    sensitive: bool = False
    visible: bool = True
    long_clickable: bool = False
    editable: bool = False

    @model_validator(mode="before")
    @classmethod
    def _alias_class(cls, data: Any) -> Any:
        # Accept "class" as the role key (task: "role/class"), then drop it
        # so extra="forbid" does not reject the payload.
        if isinstance(data, dict):
            data = dict(data)
            if "role" not in data and "class" in data:
                data["role"] = data.pop("class")
            elif "class" in data:
                data.pop("class")
        return data

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: Any) -> str:
        if not isinstance(v, str) or not v or len(v) > 128:
            raise ValueError("element id must be a non-empty string (<=128)")
        return v

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("role must be a string")
        return _clip(v, MAX_ROLE_LEN)

    @field_validator("resource_id")
    @classmethod
    def _check_resource_id(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("resource_id must be a string")
        return _clip(v, MAX_RESOURCE_ID_LEN)

    @field_validator("content_desc")
    @classmethod
    def _check_content_desc(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("content_desc must be a string")
        return _clip(v, MAX_CONTENT_DESC_LEN)

    @field_validator("text")
    @classmethod
    def _check_text(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("text must be a string")
        return _clip(v, MAX_TEXT_LEN)

    @field_validator("bounds")
    @classmethod
    def _check_bounds(cls, v: Any) -> list[int]:
        if not isinstance(v, list) or len(v) != 4:
            raise ValueError("bounds must be [4 ints]")
        out: list[int] = []
        for n in v:
            if isinstance(n, bool) or not isinstance(n, int):
                raise ValueError("bounds must be [4 ints]")
            out.append(n)
        return out

    def normalized(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "resource_id": self.resource_id,
            "content_desc": self.content_desc,
            "text": self.text,
            "bounds": list(self.bounds),
            "enabled": self.enabled,
            "selected": self.selected,
            "checked": self.checked,
            "scrollable": self.scrollable,
            "sensitive": self.sensitive,
            "visible": self.visible,
            "long_clickable": self.long_clickable,
            "editable": self.editable,
        }


# UiSnapshot: bounded container (same section — snapshot models)
# --------------------------------------------------------------------------


class UiSnapshot(BaseModel):
    """Bounded, scrubbed snapshot. snapshot_id is a stable hash of normalized
    content (excludes volatile fields: timestamp, screenshot_available)."""

    model_config = _FORBID

    snapshot_id: str = ""
    device_id: str = ""
    package_name: str = ""
    activity: str | None = None
    screen_w: int = 0
    screen_h: int = 0
    focused_id: str = ""
    elements: list[UiElement] = Field(default_factory=list)
    scrollable_regions: list[str] = Field(default_factory=list)
    keyboard_visible: bool = False
    timestamp: float = 0.0
    screenshot_available: bool = False
    truncation: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _pre_parse(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data  # let pydantic raise the top-level shape error
        data = dict(data)
        # Alias: focused_element_id -> focused_id.
        if "focused_id" not in data and "focused_element_id" in data:
            data["focused_id"] = data.pop("focused_element_id")
        elif "focused_element_id" in data:
            data.pop("focused_element_id")
        # Elements: skip malformed nodes, never raise; cap at MAX_ELEMENTS.
        raw = data.get("elements", [])
        if not isinstance(raw, list):
            raw = []
        kept: list[UiElement] = []
        truncation_info = {"elements_truncated": 0, "strings_truncated": 0, "depth_exceeded": False}
        for item in raw:
            if len(kept) >= MAX_ELEMENTS:
                truncation_info["elements_truncated"] = len(raw) - len(kept)
                break
            if not isinstance(item, dict):
                continue
            try:
                element = UiElement(**item)
                # Track string truncation
                for field_name in ["role", "resource_id", "content_desc", "text"]:
                    raw_val = item.get(field_name, "")
                    if isinstance(raw_val, str) and len(raw_val) > getattr(element, field_name).__len__():
                        truncation_info["strings_truncated"] += 1
                kept.append(element)
            except Exception:
                continue  # malformed node: skip, never raise
        data["elements"] = kept
        data["truncation"] = truncation_info
        # scrollable_regions: coerce to list of str, drop the rest.
        regs = data.get("scrollable_regions", [])
        if not isinstance(regs, list):
            regs = []
        data["scrollable_regions"] = [r for r in regs if isinstance(r, str)][:16]
        return data

    @model_validator(mode="after")
    def _assign_snapshot_id(self) -> "UiSnapshot":
        if not self.snapshot_id:
            self.snapshot_id = _snapshot_hash(self._content_digest())
        return self

    def _content_digest(self) -> dict:
        return {
            "device_id": self.device_id,
            "package_name": self.package_name,
            "activity": self.activity,
            "screen_w": self.screen_w,
            "screen_h": self.screen_h,
            "focused_id": self.focused_id,
            "keyboard_visible": self.keyboard_visible,
            "scrollable_regions": sorted(self.scrollable_regions),
            "elements": [e.normalized() for e in self.elements],
        }

    def recompute_id(self) -> str:
        """Stable content hash (same input key order or element key order
        yields the same id)."""
        return _snapshot_hash(self._content_digest())


def parse_snapshot(data: dict, *, expected_package: str | None = None,
                   expected_snapshot_id: str | None = None,
                   max_elements: int | None = None,
                   include_text: bool = True,
                   include_content_description: bool = True) -> UiSnapshot:
    """Parse untrusted snapshot input. Bad *nodes* are skipped; a bad
    top-level shape still raises.
    
    Args:
        data: Raw snapshot dictionary
        expected_package: Optional regex pattern to validate package name
        expected_snapshot_id: Optional snapshot ID for staleness check
        max_elements: Override MAX_ELEMENTS cap (1-50)
        include_text: Whether to include text field in elements
        include_content_description: Whether to include content_desc field in elements
    """
    # Validate max_elements
    if max_elements is not None:
        max_elements = max(1, min(50, int(max_elements)))
    else:
        max_elements = MAX_ELEMENTS
    
    # Capture original element count BEFORE any processing
    original_elements = data.get("elements", [])
    original_count = len(original_elements) if isinstance(original_elements, list) else 0
    
    # Parse snapshot with a temporary MAX_ELEMENTS override
    # We need to handle the cap in _pre_parse, so we temporarily modify the behavior
    # by post-processing the elements after parsing
    
    # First, extract raw elements and pre-parse them with the correct cap
    if isinstance(data, dict):
        raw_elements = data.get("elements", [])
        if isinstance(raw_elements, list) and len(raw_elements) > max_elements:
            # Truncate the raw elements list before parsing to respect max_elements
            data = dict(data)
            data["elements"] = raw_elements[:max_elements]
    
    # Parse snapshot
    snapshot = UiSnapshot(**data)
    
    # Apply field filtering if requested
    if not include_text or not include_content_description:
        filtered_elements = []
        for e in snapshot.elements:
            new_data = e.normalized()
            if not include_text:
                new_data["text"] = ""
            if not include_content_description:
                new_data["content_desc"] = ""
            filtered_elements.append(UiElement(**new_data))
        snapshot.elements = filtered_elements
    
    # Update truncation metadata to reflect actual max_elements cap
    if len(snapshot.elements) < original_count:
        snapshot.truncation["elements_truncated"] = original_count - len(snapshot.elements)
    
    return snapshot


# --------------------------------------------------------------------------
# 3. ElementTarget matcher
# --------------------------------------------------------------------------

class ElementTarget(BaseModel):
    model_config = _FORBID

    resource_id: str | None = None
    content_desc: str | None = None
    text: str | None = None
    normalized_text: str | None = None
    role: str | None = None
    index_hint: int | None = None
    x: float | None = None
    y: float | None = None


MatchStatus = Literal["MATCH", "AMBIGUOUS", "NOT_FOUND"]
MATCH: MatchStatus = "MATCH"
AMBIGUOUS: MatchStatus = "AMBIGUOUS"
NOT_FOUND: MatchStatus = "NOT_FOUND"


class MatchResult(BaseModel):
    model_config = _FORBID

    status: str = NOT_FOUND
    element: UiElement | None = None
    candidates: list[UiElement] = Field(default_factory=list)
    reason: str = ""


def _point_near_bounds(px: float, py: float, bounds: list[int],
                       tol: float = COORD_TOLERANCE_DP) -> bool:
    try:
        x1, y1, x2, y2 = bounds
        dx = max(min(x1, x2) - px, 0.0, px - max(x1, x2))
        dy = max(min(y1, y2) - py, 0.0, py - max(y1, y2))
        return math.hypot(dx, dy) <= tol
    except Exception:
        return False


def _safe_elements(snapshot: UiSnapshot) -> list[UiElement]:
    try:
        return list(snapshot.elements or [])
    except Exception:
        return []


def match(snapshot: UiSnapshot, target: ElementTarget | dict,
          expected_package: str | None = None,
          expected_snapshot_id: str | None = None) -> MatchResult:
    """Priority: resource_id > content_desc > exact text > normalized text >
    role (+ tree order, index_hint disambiguates). Coordinates only confirm:
    they require x,y AND an element within 8dp — never blind coordinates.
    
    Returns STALE_SNAPSHOT if expected_snapshot_id provided and mismatches.
    Returns UNEXPECTED_PACKAGE if expected_package provided and mismatches.
    """
    # Check stale snapshot
    if is_stale(expected_snapshot_id or "", snapshot.snapshot_id):
        return MatchResult(status="STALE_SNAPSHOT", reason="snapshot id mismatch")
    
    # Check expected package
    pkg_check = check_expected_package(snapshot, expected_package)
    if pkg_check == "UNEXPECTED_PACKAGE":
        return MatchResult(status="UNEXPECTED_PACKAGE", reason="package mismatch")
    
    try:
        if isinstance(target, dict):
            target = ElementTarget(**target)
        els = _safe_elements(snapshot)
    except Exception:
        return MatchResult(status=NOT_FOUND, reason="invalid snapshot/target")

    has_coords = (
        target.x is not None and target.y is not None
        and isinstance(target.x, (int, float)) and isinstance(target.y, (int, float))
        and not isinstance(target.x, bool) and not isinstance(target.y, bool)
    )
    criteria = (
        target.resource_id, target.content_desc, target.text,
        target.normalized_text, target.role,
    )
    if all(c is None for c in criteria) and not has_coords:
        return MatchResult(status=NOT_FOUND, reason="empty target")

    levels: list[tuple[str, list[UiElement]]] = []
    try:
        if target.resource_id is not None:
            levels.append(("resource_id", [
                e for e in els
                if isinstance(getattr(e, "resource_id", None), str)
                and e.resource_id == target.resource_id]))
        if target.content_desc is not None:
            levels.append(("content_desc", [
                e for e in els
                if isinstance(getattr(e, "content_desc", None), str)
                and e.content_desc == target.content_desc]))
        if target.text is not None:
            levels.append(("text", [
                e for e in els
                if isinstance(getattr(e, "text", None), str)
                and e.text == target.text]))
        if target.normalized_text is not None:
            want = normalize_text(target.normalized_text)
            levels.append(("normalized_text", [
                e for e in els
                if isinstance(getattr(e, "text", None), str)
                and _get_normalized_text(e) == want]))
        if target.role is not None:
            want_role = str(target.role).lower()
            levels.append(("role", [
                e for e in els
                if isinstance(getattr(e, "role", None), str)
                and e.role.lower() == want_role]))
    except Exception:
        return MatchResult(status=NOT_FOUND, reason="matcher error")

    if not levels and has_coords:
        # Coordinates alone are never a target.
        return MatchResult(status=NOT_FOUND,
                           reason="coordinates alone are not a target")

    for level_name, cands in levels:
        if not cands:
            continue  # fall through to the next priority level
        ordered = list(cands)  # snapshot tree order = structural context
        if (target.index_hint is not None and isinstance(target.index_hint, int)
                and not isinstance(target.index_hint, bool)
                and 0 <= target.index_hint < len(ordered)):
            picked = [ordered[target.index_hint]]
        elif len(ordered) == 1:
            picked = ordered
        else:
            if target.index_hint is None:
                return MatchResult(
                    status=AMBIGUOUS,
                    candidates=ordered[:5],  # capped; no hidden-state indexes
                    reason=f"ambiguous {level_name}: {len(ordered)} candidates")
            return MatchResult(status=NOT_FOUND,
                               reason=f"index_hint out of range for {level_name}")
        if has_coords:
            assert target.x is not None and target.y is not None
            near = [e for e in picked
                    if _point_near_bounds(float(target.x), float(target.y),
                                          e.bounds)]
            if not near:
                return MatchResult(
                    status=NOT_FOUND,
                    reason="no matching element within 8dp of coordinates")
            picked = near
        el = picked[0]
        suffix = f"+coordinates" if has_coords else ""
        return MatchResult(status=MATCH, element=el,
                           reason=f"matched by {level_name}{suffix}")

    if has_coords:
        return MatchResult(status=NOT_FOUND,
                           reason="no matching element within 8dp of coordinates")
    return MatchResult(status=NOT_FOUND, reason="no element matched")


def _get_normalized_text(element: UiElement) -> str:
    """Get normalized text with sensitive redaction."""
    if element.sensitive:
        return REDACTED
    return normalize_text(element.text)


# --------------------------------------------------------------------------
# 3. Privilege classes
# --------------------------------------------------------------------------

READ_CAPS = frozenset({"gui.screen.inspect", "gui.state.verify"})
INTERACT_CAPS = frozenset({
    "gui.tap", "gui.scroll", "gui.text_input", "gui.app.launch",
})
INTERACT_PREFIXES = ("gui.navigation.",)
SENSITIVE_SUFFIXES = (".send", ".upload", ".download", ".share", ".submit")
DESTRUCTIVE_SUFFIXES = (".delete", ".uninstall", ".purchase")
DESTRUCTIVE_SUBSTRINGS = ("security", "account")

READ = "READ"
INTERACT = "INTERACT"
SENSITIVE = "SENSITIVE"
DESTRUCTIVE = "DESTRUCTIVE"
UNKNOWN = "UNKNOWN"


def class_of(capability_id: str) -> str:
    """Map a capability id to its privilege class. Destructive patterns win
    over sensitive ones (e.g. "vault.account.send" is DESTRUCTIVE)."""
    if not isinstance(capability_id, str) or not capability_id:
        return UNKNOWN
    cid = capability_id.lower()
    if capability_id in READ_CAPS or cid in READ_CAPS:
        return READ
    if (cid.endswith(DESTRUCTIVE_SUFFIXES)
            or any(s in cid for s in DESTRUCTIVE_SUBSTRINGS)):
        return DESTRUCTIVE
    if cid.endswith(SENSITIVE_SUFFIXES):
        return SENSITIVE
    if capability_id in INTERACT_CAPS or cid in INTERACT_CAPS:
        return INTERACT
    if cid.startswith(INTERACT_PREFIXES):
        return INTERACT
    return UNKNOWN


# --------------------------------------------------------------------------
# 4. StuckDetector (deterministic, no LLM)
# --------------------------------------------------------------------------

PROGRESS = "PROGRESS"
REPEAT_SNAPSHOT = "REPEAT_SNAPSHOT"
REPEAT_ACTION = "REPEAT_ACTION"
PACKAGE_JUMP = "PACKAGE_JUMP"
DIALOG = "DIALOG"
CRASH = "CRASH"
STOP = "STOP"


class StuckDetector:
    """Tracks (snapshot_id, action) history and classifies the run state."""

    def __init__(self, target_package: str = "",
                 max_steps: int = MAX_STEPS) -> None:
        self.target_package = target_package or ""
        self.max_steps = max_steps
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history.clear()

    @property
    def steps(self) -> int:
        return len(self.history)

    def observe(self, snapshot: UiSnapshot | dict, action: str = "") -> str:
        try:
            if isinstance(snapshot, dict):
                sid = str(snapshot.get("snapshot_id", ""))
                pkg = str(snapshot.get("package_name", "") or "")
            else:
                sid = str(getattr(snapshot, "snapshot_id", "") or "")
                pkg = str(getattr(snapshot, "package_name", "") or "")
        except Exception:
            sid, pkg = "", ""
        if not isinstance(action, str):
            action = str(action)
        self.history.append({"snapshot_id": sid, "action": action,
                             "package": pkg})
        if len(self.history) > self.max_steps:
            return STOP
        if pkg in DIALOG_PACKAGES:
            return DIALOG
        if (self.target_package and pkg != self.target_package
                and pkg in CRASH_PACKAGES):
            return CRASH
        if self.target_package:
            if pkg != self.target_package and pkg not in DIALOG_PACKAGES:
                return PACKAGE_JUMP
        elif len(self.history) >= 2:
            prev = self.history[-2]["package"]
            if (pkg != prev and pkg not in DIALOG_PACKAGES
                    and prev not in DIALOG_PACKAGES):
                return PACKAGE_JUMP
        if len(self.history) >= 3:
            last3 = self.history[-3:]
            sids = [h["snapshot_id"] for h in last3]
            acts = [h["action"] for h in last3]
            if acts[0] == acts[1] == acts[2] and sids[0] == sids[1] == sids[2]:
                return REPEAT_ACTION  # same action, no effect
            if sids[0] == sids[1] == sids[2]:
                return REPEAT_SNAPSHOT  # frozen UI, varying actions
        return PROGRESS


# --------------------------------------------------------------------------
# 5. Registration guard evaluator (pure function)
# --------------------------------------------------------------------------

def evaluate_probe(probe: dict) -> tuple[bool, str]:
    """Supported ONLY if service_enabled + can_retrieve + can_act are truthy,
    api_level is present, ping_ms is present and < 2000. Deterministic."""
    if not isinstance(probe, dict):
        return (False, "probe must be an object")
    if not probe.get("service_enabled"):
        return (False, "service disabled")
    if not probe.get("can_retrieve"):
        return (False, "retrieval unavailable")
    if not probe.get("can_act"):
        return (False, "action unavailable")
    if probe.get("api_level") is None:
        return (False, "api_level missing")
    ping = probe.get("ping_ms")
    if ping is None:
        return (False, "ping_ms missing")
    if isinstance(ping, bool) or not isinstance(ping, (int, float)):
        return (False, "ping_ms invalid")
    if not math.isfinite(float(ping)):
        return (False, "ping_ms invalid")
    if float(ping) >= MAX_PROBE_PING_MS:
        return (False, f"ping too high: {ping}ms >= 2000ms")
    return (True, "supported")


# --------------------------------------------------------------------------
# 6. ActionVerify structures
# --------------------------------------------------------------------------

class PlannedAction(BaseModel):
    model_config = _FORBID

    capability: str = Field(min_length=1, max_length=128)
    target: ElementTarget = Field(default_factory=ElementTarget)
    timeout_s: float = Field(default=10.0, gt=0, le=MAX_TIMEOUT_S)
    retry: int = Field(default=0, ge=0, le=MAX_RETRY)
    snapshot_before_id: str = ""


_REINSPECT_STATUSES = frozenset(
    {"stale", "ambiguous", "screen_changed", "reinspect"})


def needs_reinspect(result: dict) -> bool:
    """True when the action result shows the screen moved under us (stale /
    changed snapshot ids, reinspect flags, or stale-ish statuses)."""
    if not isinstance(result, dict):
        return True  # fail closed: unknown shape -> look again
    if result.get("reinspect") or result.get("stale"):
        return True
    if result.get("snapshot_changed"):
        return True
    before = result.get("snapshot_before_id")
    after = result.get("snapshot_after_id")
    if (isinstance(before, str) and isinstance(after, str)
            and before and after and before != after):
        return True
    status = result.get("status")
    if isinstance(status, str) and status.lower() in _REINSPECT_STATUSES:
        return True
    return False
