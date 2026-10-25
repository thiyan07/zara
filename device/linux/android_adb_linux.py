"""Stage 28 — host-side Android inventory for the Linux agent.

Exposes ONE bounded, detection-only capability: `system.android.inventory`.
It answers "which Android devices does this laptop actually see over
adb right now?" — serial/state/model metadata only, capped like every
other adb surface (device/adb_linux.py, the Stage-24b read-only layer
this module wraps).

Boundaries (unchanged policy, restated so nobody has to re-derive it):
- DISCOVERY IS NOT PERMISSION. A device appearing here changes nothing
  about authorization: every interaction still flows through the
  paired-device capability path (gui.*/android.* proxies, per-action
  Core authorization, bounded device jobs).
- Truthful absence: "adb not installed" and "adb present, nothing
  attached" are honest detection results (adb=false with the exact
  reason / adb=true, count=0) — never a fabricated device, never a
  hang. The result contract (core.device_tools.ANDROID_OUTPUT_SCHEMAS)
  forces counters and the refusal reason into every response.
- One bounded adb call per execution (subprocess timeout inside the
  wrapped layer); output strings are size-capped there. Nothing is
  persisted, nothing leaves the host, no permissions are touched.
"""
from __future__ import annotations

from core.models import PermissionLevel, RiskLevel, ToolDefinition
from core.device_tools import ANDROID_OUTPUT_SCHEMAS
from device import adb_linux

# Test seams: the ONLY entry points into the adb layer, so the real
# process layer is faked without touching the handler logic.
_detect = adb_linux.adb_available
_list = adb_linux.list_devices


def _def(name: str, desc: str, schema: dict, caps: list[str], cost: float,
         perm=PermissionLevel.RESTRICTED, risk=RiskLevel.SAFE,
         timeout: float = 15.0, output_schema: dict | None = None,
         verification: str = "none", max_output_bytes: int = 0
         ) -> ToolDefinition:
    return ToolDefinition(name=name, description=desc, input_schema=schema,
                          output_schema=output_schema or {"type": "object"},
                          required_capabilities=caps, permission=perm,
                          risk=risk, estimated_cost=cost, timeout_s=timeout,
                          success_criteria="structured result returned",
                          failure_behavior="fail", verification=verification,
                          supported_devices=["linux"], reversible=True,
                          version="1.0.0",
                          max_output_bytes=max_output_bytes)


def android_devices(inputs: dict, ctx: dict) -> dict:
    """Detection-only inventory. Bounded, honest, never fabricated."""
    check = _detect()
    if not check.get("ok"):
        return {"adb": False, "path": "",
                "reason": str(check.get("reason", "adb unavailable"))[:200],
                "devices": [], "count": 0, "stage": "detection"}
    r = _list()
    devices = r.get("devices", []) if r.get("ok") else []
    if not isinstance(devices, list):
        devices = []
    stage = str(r.get("stage", "detection"))
    reason = "" if r.get("ok") else str(r.get("reason", "adb devices " \
        "failed"))[:200]
    out = {"adb": bool(r.get("ok")),
           "path": str(check.get("path", ""))[:128],
           "reason": reason,
           "devices": devices,
           "count": len(devices),
           "stage": stage}
    if out["adb"] and out["count"] == 0:
        out["note"] = ("adb is available but no Android device is "
                       "currently attached")
    elif out["adb"] and out["count"] > 0:
        out["note"] = ("serials are observation metadata; visibility "
                       "is not authorization")
    return out


ANDROID_ADB_TOOLS: list[tuple[ToolDefinition, object]] = [
    (_def("system.android.inventory",
          "List Android devices currently visible to adb on the laptop "
          "(bounded inventory: serial, state, model). Honest detection: "
          "adb missing and no-device-attached are reported as such — "
          "never a fake success. Read-only observation; never an "
          "action permission.",
          {"type": "object", "properties": {}},
          ["system.android.inventory"], 0.05, PermissionLevel.PUBLIC,
          RiskLevel.SAFE, timeout=20.0,
          output_schema=ANDROID_OUTPUT_SCHEMAS["system.android.inventory"],
          verification="output_schema", max_output_bytes=65536),
     android_devices),
]
