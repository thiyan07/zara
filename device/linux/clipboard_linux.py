"""Consent-based clipboard access for the Linux agent (Stage 25).

A deliberately tiny interface: read the CURRENT clipboard text, write a
bounded text result — each individually authorized through the existing
Core policy (RESTRICTED permission, CONFIRM risk → a fresh scoped grant
each time), audited as metadata without content.

Hard boundaries (this module never becomes a clipboard logger):
- No background polling, no clipboard-history collection, no watching.
- No persistence: text is never written to disk by these handlers; it
  lives only inside the (in-memory) execution result for this one call.
- Content is never echoed into error messages, exception strings, or
  audit output; engine events carry ids, not payloads.
- Strict size caps both directions; oversized writes are refused, not
  truncated silently.
- Honest backend reporting: a missing wl-paste/wl-copy/xclip/xsel or a
  headless session reports ``supported: False`` — success is never
  faked. Write verification is a read-back comparison, and ``verified``
  is only True when the read-back matches exactly.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from core.models import PermissionLevel, RiskLevel, ToolDefinition

# Strict caps (documented; the write definition raises the registry
# input envelope to mirror exactly this handler contract — never wider).
MAX_CLIPBOARD_TEXT = 100_000          # chars; writes beyond this refused
READ_TIMEOUT_S = 5.0                  # one bounded attempt, no hanging
WRITE_TIMEOUT_S = 5.0


class ClipboardError(ValueError):
    """Honest clipboard refusal (unsupported/empty/oversized/failed)."""


def _def(name: str, desc: str, schema: dict, caps: list[str], cost: float,
         timeout: float = 20.0, max_input_bytes: int = 4096,
         verification: str = "explicit") -> ToolDefinition:
    # Stage 27: `explicit` — the handlers self-report read-back truth in
    # result["verified"], and the engine records it honestly instead of
    # the previous "none" mode latching verified=True at the record
    # level. `verified: None` (a read has no read-back) stays honest:
    # executed, not verified-by-design.
    return ToolDefinition(name=name, description=desc,
                          input_schema=schema,
                          output_schema={"type": "object"},
                          required_capabilities=caps,
                          permission=PermissionLevel.RESTRICTED,
                          risk=RiskLevel.CONFIRM, estimated_cost=cost,
                          timeout_s=timeout,
                          success_criteria="structured result returned",
                          failure_behavior="fail", verification=verification,
                          supported_devices=["linux"], reversible=True,
                          version="1.0.0",
                          max_input_bytes=max_input_bytes)


def _detect_session() -> str:
    """Graphical session type from the environment (test-monkeypatchable)."""
    xdg = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if xdg in ("x11", "wayland"):
        return xdg
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "headless"


_DETECT = _detect_session          # monkeypatch targets for fixture tests
_WHICH = shutil.which


# (read_cmd, write_cmd) per session. Read commands use --no-newline where
# offered so a trailing newline is never mistaken for clipboard content.
_BACKENDS = {
    "wayland": (["wl-paste", "--no-newline", "-n"],
                ["wl-copy"]),
    "x11": (["xclip", "-o", "-selection", "clipboard"],
            ["xclip", "-selection", "clipboard"]),
}


def _backend(session: str, op: str):
    """First available backend command for the session, else None."""
    cmds = _BACKENDS.get(session)
    if not cmds:
        return None
    read_cmd, write_cmd = cmds
    cmd = read_cmd if op == "read" else write_cmd
    if _WHICH(cmd[0]) is None:
        return None
    return cmd


def _first_backend_name(session: str, op: str = "read") -> str:
    cmds = _BACKENDS.get(session)
    if not cmds:
        return "unknown"
    return cmds[0 if op == "read" else 1][0]


def _run_bounded(cmd: list[str], timeout: float, input_bytes: bytes = b""):
    """Run a clipboard backend: shell=False, bounded capture, stderr kept
    content-free for honest errors. Returns (rc, out, err)."""
    try:
        proc = subprocess.run(cmd, input=input_bytes, capture_output=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr or b""
    except FileNotFoundError:
        return 127, b"", b"backend binary disappeared"
    except subprocess.TimeoutExpired:
        return 124, b"", b"clipboard backend timed out"
    except OSError as e:
        return 126, b"", f"backend failed: {type(e).__name__}".encode()


def clipboard_read(inputs: dict, ctx: dict) -> dict:
    """Read current clipboard text. Sensitive by nature: requires a
    fresh CONFIRM grant each time; the text is returned ONCE in the
    result and never logged by this module."""
    session = _DETECT()
    if session == "headless":
        return {"supported": False, "session": session, "reason":
                "no graphical session running", "text": None,
                "logical_op": "read"}
    cmd = _backend(session, "read")
    if cmd is None:
        return {"supported": False, "session": session, "reason":
                "no clipboard backend installed "
                f"(looked for {_first_backend_name(session)})",
                "text": None, "logical_op": "read"}
    rc, out, _err = _run_bounded(cmd, READ_TIMEOUT_S)
    if rc != 0:
        if rc <= 1 and not out:
            # xclip convention: exit 1 with no output ≈ empty clipboard.
            # Reported honestly; never a success pretense.
            return {"supported": True, "backend": session, "session":
                    session, "text": None, "chars": 0, "empty": True,
                    "truncated": False, "verified": None}
        # Other nonzero states (timeout, backend crash) are neither
        # empty nor a success; stderr is never echoed (content-free).
        return {"supported": True, "backend": session, "session": session,
                "text": None, "chars": 0, "empty": None, "truncated":
                False, "failed": True, "verified": None,
                "reason": f"clipboard backend failed (rc={rc})"}
    text = out.decode("utf-8", "replace")[: (MAX_CLIPBOARD_TEXT + 1)]
    return {"supported": True, "backend": session, "session": session,
            "text": text[:MAX_CLIPBOARD_TEXT],
            "chars": min(len(text), MAX_CLIPBOARD_TEXT),
            "empty": text == "",
            "truncated": len(text) > MAX_CLIPBOARD_TEXT,
            "verified": None}


def clipboard_write(inputs: dict, ctx: dict) -> dict:
    """Write bounded text to the clipboard (overwrite). Verified by
    read-back; oversized text refused, never silently truncated."""
    text = inputs.get("text")
    if not isinstance(text, str):
        raise ClipboardError("text must be a string")
    if not text:
        raise ClipboardError("refusing to write empty text (ambiguous)")
    if len(text) > MAX_CLIPBOARD_TEXT:
        raise ClipboardError(
            f"text exceeds the {MAX_CLIPBOARD_TEXT}-character clipboard "
            "write cap (refused, not truncated)")
    session = _DETECT()
    if session == "headless":
        return {"supported": False, "session": session, "reason":
                "no graphical session running", "verified": None,
                "logical_op": "copy"}
    cmd = _backend(session, "write")
    if cmd is None:
        return {"supported": False, "session": session, "reason":
                "no clipboard backend installed "
                f"(looked for {_first_backend_name(session, 'write')})",
                "verified": None, "logical_op": "copy"}
    rc, _out, _err = _run_bounded(cmd, WRITE_TIMEOUT_S,
                                  input_bytes=text.encode("utf-8"))
    if rc != 0:
        return {"supported": True, "backend": session, "session": session,
                "verified": False, "chars": len(text), "reason":
                "backend exited nonzero (not written)",
                "logical_op": "copy"}
    # Verify by bounded read-back; only an exact match claims success.
    rcmd = _backend(session, "read")
    if rcmd is None:
        return {"supported": True, "backend": session, "session": session,
                "chars": len(text), "verified": None, "reason":
                "write ok; read-back unavailable to verify",
                "logical_op": "copy"}
    rc2, out2, _ = _run_bounded(rcmd, READ_TIMEOUT_S)
    back = out2.decode("utf-8", "replace")[:MAX_CLIPBOARD_TEXT]
    return {"supported": True, "backend": session, "session": session,
            "chars": len(text), "verified": (rc2 == 0 and back == text),
            "empty": False, "truncated": False, "logical_op": "copy"}


CLIPBOARD_TOOLS = [
    (_def("desktop.clipboard.read",
          "Read the CURRENT clipboard text once, when authorized. No "
          "polling, no history, no persistence; content is never logged.",
          {"type": "object", "properties": {}},
          ["desktop.clipboard.read"], 0.1), clipboard_read),
    (_def("desktop.clipboard.write",
          "Write bounded text to the clipboard (overwrite), verified by "
          "read-back. Authorized explicitly each time.",
          {"type": "object", "required": ["text"],
           "properties": {"text": {"type": "string", "minLength": 1}}},
          ["desktop.clipboard.write"], 0.1,
          max_input_bytes=131072), clipboard_write),
]
