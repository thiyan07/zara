"""Stage 21 — cross-device file missions.

``FileTransferMissionRunner`` composes the EXISTING primitives into
end-to-end file missions; it creates no second transfer engine:

- same-device hops (laptop <-> pendrive) run the agent-side
  ``storage.copy``/``storage.move`` tools. Every step is submitted
  through ``ExecutionEngine.submit`` with the mission bound, so each
  hop re-decides policy, parks for approval, dispatches via fabric,
  verifies, and audits — the same funnel a natural-language request
  uses.
- cross-device hops use the Stage-15 relay through two new agent-side
  tools so BYTES NEVER travel through Core tool envelopes:
  ``storage.xfer_send`` (agent reads a source file within its allowed
  roots or removable mounts, computes the digest, requests+uploads as
  the sender identity) and ``storage.xfer_fetch`` (agent polls its
  relay pending list, downloads, verifies the digest, writes to a
  destination). The relay's own chain (trust/presence/policy/
  governor/concurrency/digest/recipient-ack) still stands.
- long-term state + restart recovery come from
  ``PersistentMissionEngine`` checkpoints; the relay rehydrates its
  records after a Core restart. ``resume()`` re-enters steps that are
  still pending; steps already ok are never re-run (idempotent).

Truthfulness rules: a partially-copied multi-file mission is FAILED
(with per-item evidence), never COMPLETED. Disconnection mid-transfer
fails the affected item and preserves sources. Ambiguous targets are
WAITING with explicit options — never guessed. The 5 MiB relay
ceiling is reported as a precise limitation.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Callable, Optional

from .models import MissionState

KINDS_SOURCE = ("laptop", "phone")
XFER_MAX_BYTES = 5 * 1024 * 1024          # relay single-transfer ceiling
STAGING_DIR = "/tmp/zara-transfer-staging"


class MissionSpecError(ValueError):
    pass


class _InFlightFail(RuntimeError):
    """Raised by _fail() when a step fails outright mid-execution; run()
    converts it into the mission's FAILED transition exactly once."""
    pass


def validate_spec(spec: dict) -> dict:
    """Strict validation; raises MissionSpecError with a fix-list."""
    if not isinstance(spec, dict):
        raise MissionSpecError("spec must be an object")
    src = spec.get("source")
    dst = spec.get("dest")
    for name, block in (("source", src), ("dest", dst)):
        if not isinstance(block, dict):
            raise MissionSpecError(f"missing '{name}' block")
        if (block.get("device") or "").strip() not in KINDS_SOURCE:
            raise MissionSpecError(
                f"{name}.device must be one of {list(KINDS_SOURCE)}")
    items = spec.get("items")
    if spec.get("fixture_share_hint") is None and \
            (not isinstance(items, list) or not items or len(items) > 200):
        raise MissionSpecError(
            "items must be a list of 1..200 file names/paths")
    for it in (items or []):
        if not isinstance(it, str) or not it.strip().strip("/"):
            raise MissionSpecError("each item must be a non-empty name")
        if any(t in it for t in ("..", "\x00")):
            raise MissionSpecError("names must not traverse upward")
    if not isinstance(spec.get("move", False), bool) or \
            not isinstance(spec.get("delete_source_after", False), bool):
        raise MissionSpecError("flags must be booleans")
    if spec.get("delete_source_after") and not spec.get("move"):
        raise MissionSpecError(
            "delete_source_after is only valid with move=true")
    if not isinstance(spec.get("dest").get("path", ""), str):
        raise MissionSpecError("dest.path must be a string")
    return spec


def describe(spec: Optional[dict]) -> str:
    if not spec:
        return "file transfer (details requested)"
    src, dst = spec.get("source", {}), spec.get("dest", {})
    move = "move" if spec.get("move") else "copy"
    n = len(spec.get("items") or [])
    return (f"{move} {n} file(s) from {src.get('device', '?')}"
            f"{':' + str(src.get('path', ''))[:40] if src.get('path') else ''}"
            f" to {dst.get('device', '?')}"
            f"{':' + str(dst.get('path', ''))[:40] if dst.get('path') else ''}")


class FileTransferMissionRunner:
    """Runs file-transfer missions through the existing engines."""

    def __init__(self, missions, xfer, bus=None, now=time.time,
                 execute: Optional[Callable] = None,
                 resolve_device: Optional[Callable] = None,
                 audit: Optional[Callable] = None,
                 wait_s: float = 180.0, poll_s: float = 1.5) -> None:
        self._missions = missions          # (Persistent)MissionEngine
        self._xfer = xfer                  # TransferEngine
        self._bus = bus                    # publish(type, payload)
        self._now = now
        self._execute = execute            # (tool, inputs, device_id) -> dict
        self._resolve_device = resolve_device  # hint -> (dev, matched)
        self._wait_s = wait_s
        self._poll_s = poll_s
        self._audit_fn = audit
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._specs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------- mission control --------------------------------------

    def start(self, spec: Optional[dict], who: str = "user",
              autostart: bool = True) -> dict:
        """Create the persistent mission; with no spec it enters
        WAITING and asks for specifics (never guesses)."""
        if spec is None:
            m = self._missions.create(describe(None),
                                      steps=[{"tool": "await_spec",
                                              "inputs": {}}])
            # WAITING is only reachable from EXECUTING: establish the
            # full lifecycle so postponing specifics is a legal state.
            for to_state in (MissionState.PLANNING, MissionState.EXECUTING,
                             MissionState.WAITING):
                try:
                    self._missions.transition(m.id, to_state)
                except Exception:
                    pass
            self._emit(m.id, {"waiting": True,
                              "questions": [
                                  "which files (source device + folder)",
                                  "where to (destination device + "
                                  "drive/folder)"]})
            return {"mission_id": m.id, "state": m.state.value}
        spec = validate_spec(dict(spec))
        m = self._missions.create(describe(spec), steps=[], device_id=None)
        self._missions.transition(m.id, MissionState.PLANNING)
        self._write_spec(m.id, spec)
        self._audit("transfer_mission_created", m.id, describe(spec))
        if autostart:
            self.launch(m.id)
        return {"mission_id": m.id, "goal": m.goal, "state": m.state.value}

    def cancel(self, mission_id: str, who: str = "user") -> dict:
        """Operator cancellation: stops between items, cancels any
        relay transfers tagged with this mission, transitions. An
        in-flight item finishes; the report stays truthful."""
        ev = self._cancel.setdefault(mission_id, threading.Event())
        ev.set()
        out = {"cancelled_flag": True}
        try:
            for rec in self._xfer.list():
                if (rec.metadata or {}).get("mission_id") == mission_id \
                        and rec.state not in ("succeeded", "failed",
                                              "cancelled", "rejected",
                                              "expired"):
                    try:
                        self._xfer.cancel(rec.transfer_id, who)
                        out.setdefault("xfer_cancelled", []).append(
                            rec.transfer_id)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            m = self._missions.get(mission_id)
            if m.state.value not in ("completed", "failed"):
                self._missions.transition(mission_id, MissionState.CANCELLED)
        except Exception:
            pass
        self._audit("transfer_mission_cancelled", mission_id, who)
        return out

    def launch(self, mission_id: str) -> None:
        with self._lock:
            th = self._threads.get(mission_id)
            if th is not None and th.is_alive():
                return
            th = threading.Thread(target=self.run, args=(mission_id,),
                                  daemon=True)
            self._threads[mission_id] = th
            th.start()

    def resume(self, mission_id: str, spec_patch: dict | None = None,
               who: str = "user") -> dict:
        """Continue a waiting mission (or re-enter after approvals).
        Ambiguity answers arrive as spec patches; already-ok steps are
        never re-run."""
        try:
            m = self._missions.get(mission_id)
        except KeyError:
            return {"error": "unknown mission"}
        if spec_patch:
            s = {"spec": dict(self._read_spec(mission_id))}
            base = s.setdefault("spec", {})
            for k in ("source", "dest"):
                if isinstance(patch := spec_patch.get(k), dict):
                    base[k] = {**(base.get(k) or {}), **patch}
            for k in ("move", "delete_source_after"):
                if k in spec_patch:
                    base[k] = bool(spec_patch[k])
            if isinstance(items := spec_patch.get("items"), list):
                base["items"] = items
            try:
                validate_spec(base)
            except MissionSpecError as e:
                return {"error": f"invalid continuation: {e}"}
            self._write_spec(mission_id, base)
        if m.state.value in ("completed", "failed", "cancelled"):
            return {"state": m.state.value, "note": "terminal"}
        ev = self._cancel.get(mission_id)
        if ev is not None:
            ev.clear()
        self.launch(mission_id)
        return {"state": "scheduled"}

    # ---------- execution --------------------------------------------

    def run(self, mission_id: str) -> None:
        try:
            m = self._missions.get(mission_id)
        except KeyError:
            return
        if m.state.value in ("completed", "failed", "cancelled"):
            return
        for from_state, to_state in (
                ("created", MissionState.PLANNING),
                ("planning", MissionState.EXECUTING),
                ("waiting", MissionState.EXECUTING),
                ("waiting_for_permission", MissionState.EXECUTING)):
            if m.state.value == from_state:
                try:
                    self._missions.transition(mission_id, to_state)
                except Exception:
                    pass
        try:
            self._execute_steps(mission_id)
        except _InFlightFail as e:
            try:
                self._missions.transition(mission_id, MissionState.FAILED,
                                          error=str(e)[:280])
            except Exception:
                pass
            self._emit(mission_id, {"failed": str(e)[:200]})
        except Exception as e:  # noqa: BLE001 — the runner never crashes out
            try:
                self._missions.transition(mission_id, MissionState.FAILED,
                                          error=f"runner error: {e}"[:280])
            except Exception:
                pass
            self._emit(mission_id, {"failed": f"runner error: {e}"[:200]})

    # ---------- steps --------------------------------------------------

    def _execute_steps(self, mission_id: str) -> None:
        spec = self._read_spec(mission_id)
        if not spec:
            return self._ask_for_spec(mission_id)
        try:
            validate_spec(spec)
        except MissionSpecError as e:
            return self._enter_wait(
                mission_id, reason=f"invalid spec: {e}", options=[])
        src, dst = spec["source"], spec["dest"]
        items = list(spec.get("items") or [])
        src_dev, src_ok = self._resolve_device(src.get("device", ""))
        dst_dev, dst_ok = self._resolve_device(dst.get("device", ""))
        if not src_ok or not dst_ok:
            return self._enter_wait(
                mission_id,
                reason="unknown device hint — options below",
                options=[{"question": "source device",
                          "options": list(KINDS_SOURCE)}
                         if not src_ok else None,
                         {"question": "destination device",
                          "options": list(KINDS_SOURCE)}
                         if not dst_ok else None])
        hop = f"{src['device']}->{dst['device']}"
        if hop == "phone->phone":
            return self._fail(mission_id, "phone->phone is not a "
                                         "supported mission path")
        # destination disambiguation for laptop-sides
        if dst["device"] == "laptop" and not dst.get("path"):
            res = self._resolve_dest(mission_id, dst_dev)
            if res.get("waiting"):
                return
            dst["path"] = res["mountpoint"]
            dst["path_explicit"] = False
        # capacity precheck (observed before any copy; evidence is
        # checkpointed). The storage.* tools still re-check per item at
        # execution time — this is the mission-level honest early look.
        precheck: dict = {}
        if dst["device"] == "laptop":
            try:
                cap_res = self._execute(
                    "storage.capacity", {"mount": dst["path"]},
                    dst_dev.device_id) or {}
                fb = cap_res.get("free_bytes")
                precheck["free_bytes"] = int(fb) if fb is not None \
                    else None
            except Exception as e:  # noqa: BLE001 — honest absence
                precheck["free_bytes"] = None
                precheck["error"] = str(e)[:120]
            if precheck["free_bytes"] is None or \
                    precheck["free_bytes"] <= 0:
                self._enter_wait(
                    mission_id,
                    reason="destination capacity unavailable — "
                           "refusing to copy without a reading",
                    options=[])
                self._write_spec(mission_id, spec)
                return
            m = self._missions.get(mission_id)
            if all(c.get("kind") != "capacity_precheck"
                   for c in m.checkpoints):
                m.checkpoints.append(
                    {"kind": "capacity_precheck",
                     "free_bytes": precheck["free_bytes"],
                     "mount": dst["path"]})
                if hasattr(self._missions, "_save"):
                    self._missions._save(m)
        # destination disambiguation for laptop-sides
        if dst["device"] == "laptop" and not dst.get("path"):
            res = self._resolve_dest(mission_id, dst_dev)
            if res.get("waiting"):
                return
            dst["path"] = res["mountpoint"]
            dst["path_explicit"] = False
        manifest = []
        for fini in ("resolve", "inspect"):
            self._mark(mission_id, fini)
        if hop == "phone->laptop":
            manifest = self._hop_phone_to_laptop(mission_id, src_dev,
                                                 dst_dev, dst["path"],
                                                 items)
        else:
            move = bool(spec.get("move"))
            if hop == "laptop->phone":
                manifest = _hop_laptop_to_phone(
                    self, mission_id, src_dev, dst_dev,
                    src.get("path") or "", items, move)
            else:
                manifest = self._hop_local(mission_id, src_dev, dst_dev,
                                           src.get("path") or "", items,
                                           dst["path"], move)
        self._mark(mission_id, "hop")
        bad = [e for e in manifest if not e.get("verified")]
        ok = [e for e in manifest if e.get("verified")]
        if bad and all(e.get("waiting") for e in bad):
            # every outstanding item is "waiting for the phone to
            # share" — a resumable pause, not a failure
            reason = "; ".join(
                str(e.get("error"))[:100] for e in bad[:3])[:240]
            self._enter_wait(mission_id, reason=reason, options=[
                {"question": "share the files from the phone's Zara "
                             "app, then resume the mission",
                 "options": []}])
            self._write_spec(mission_id, spec)
            return
        if bad:
            detail = "; ".join(f"{b.get('name')}: "
                               f"{b.get('error') or 'unverified'}"
                               for b in bad)[:240]
            try:
                self._missions.transition(
                    mission_id, MissionState.FAILED,
                    error=f"{len(ok)}/{len(manifest)} verified — "
                          f"{detail}")
            except Exception:
                pass
            self._emit(mission_id, {"partial": True,
                                    "copied": len(ok),
                                    "failed": len(bad),
                                    "manifest": manifest})
            self._audit("transfer_mission_failed", mission_id,
                        f"{len(ok)}/{len(manifest)} verified")
            return
        try:
            self._missions.transition(mission_id, MissionState.VERIFYING)
            self._missions.transition(mission_id, MissionState.COMPLETED)
        except Exception:
            pass
        self._emit(mission_id, {"completed": True,
                                "copied": len(ok),
                                "manifest": manifest})
        self._audit("transfer_mission_completed", mission_id,
                    f"{len(ok)}/{len(manifest)}")

    # ---- helpers ------------------------------------------------------

    def _mark(self, mission_id: str, name: str) -> None:
        try:
            m = self._missions.get(mission_id)
            for s in m.steps:
                if s.tool == name and s.status == "pending":
                    s.status = "ok"
                    break
        except Exception:
            pass

    def _write_spec(self, mission_id: str, spec: dict) -> None:
        self._specs[mission_id] = {"spec": spec}
        try:
            m = self._missions.get(mission_id)
            # spec survives restarts: checkpointed on the mission model
            # (PersistentMissionEngine saves checkpoints to sqlite when
            # configured). Replace any earlier spec checkpoint.
            m.checkpoints = [c for c in m.checkpoints
                             if c.get("kind") != "transfer_spec"]
            m.checkpoints.append({"kind": "transfer_spec", "spec": spec})
            if hasattr(self._missions, "_save"):
                self._missions._save(m)
        except Exception:
            pass

    def _read_spec(self, mission_id: str) -> dict:
        s = self._specs.get(mission_id)
        if s and s.get("spec"):
            return dict(s["spec"])
        try:
            m = self._missions.get(mission_id)
            for c in reversed(m.checkpoints):
                if c.get("kind") == "transfer_spec":
                    return dict(c.get("spec") or {})
        except Exception:
            pass
        return {}

    def _ask_for_spec(self, mission_id: str) -> None:
        self._enter_wait(mission_id,
                         reason="mission needs specifics",
                         options=[
                             {"question": "Which files?",
                              "options": ["source device + folder/files"]},
                             {"question": "Where to?", "options":
                              ["destination device + drive/folder"]}])

    def _enter_wait(self, mission_id: str, reason: str,
                    options: list) -> None:
        try:
            m = self._missions.get(mission_id)
            if m.state == MissionState.WAITING:
                pass
            else:
                self._missions.transition(mission_id, MissionState.WAITING)
        except Exception:
            pass
        self._emit(mission_id, {"waiting": True, "reason": reason,
                                "options": [o for o in options if o]})

    def _fail(self, mission_id: str, reason: str) -> Optional[None]:
        try:
            self._missions.transition(mission_id, MissionState.FAILED,
                                      error=reason[:280])
        except Exception:
            pass
        self._emit(mission_id, {"failed": reason[:200]})
        self._audit("transfer_mission_failed", mission_id, reason[:120])

    def _resolve_dest(self, mission_id: str, dst_dev) -> dict:
        if not dst_dev:
            self._enter_wait(mission_id,
                             reason="destination device offline",
                             options=[])
            return {"waiting": True}
        try:
            res = self._execute("storage.volumes", {},
                                dst_dev.device_id, mission_id)
            vols = [v for v in ((res or {}).get("volumes") or [])
                    if v.get("mountpoint") and not v.get("read_only")]
        except Exception:
            vols = []
        if not vols:
            self._enter_wait(
                mission_id,
                reason="no writable removable storage mounted on the "
                       "destination right now",
                options=[])
            return {"waiting": True}
        if len(vols) > 1:
            self._enter_wait(
                mission_id, reason="multiple removable volumes mounted",
                options=[{"question": "Which destination volume?",
                          "options": [v["mountpoint"] for v in vols]}])
            return {"waiting": True}
        return {"mountpoint": vols[0]["mountpoint"]}

    def _cancelled(self, mission_id: str) -> bool:
        ev = self._cancel.get(mission_id)
        return bool(ev and ev.is_set())

    def _emit(self, mission_id: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish("mission_progress",
                              {"mission_id": mission_id,
                               "kind": "file_transfer", **payload})
        except Exception:
            pass

    def _audit(self, action: str, target: str, detail: str = "") -> None:
        if self._audit_fn is not None:
            try:
                self._audit_fn(action, target, detail[:240])
            except Exception:
                pass

    # ---- hops ---------------------------------------------------------

    def _hop_local(self, mission_id, src_dev, dst_dev, src_path, items,
                   dst_dir, move: bool) -> list[dict]:
        tool = "storage.move" if move else "storage.copy"
        dev = dst_dev.device_id if dst_dev else src_dev.device_id
        manifest = []
        total = len(items)
        for i, name in enumerate(items):
            if self._cancelled(mission_id):
                manifest.append({"name": name, "verified": False,
                                 "error": "cancelled before this item",
                                 "cancelled": True})
                continue
            src_full = (src_path.rstrip("/") + "/" +
                        name.strip().lstrip("/")) if src_path else name
            dst_full = dst_dir.rstrip("/") + "/" + os.path.basename(name)
            try:
                res = self._execute(tool, {"src": src_full,
                                           "dst": dst_full,
                                           "on_conflict": "ask"}, dev) or {}
                done = bool(res.get("copied") or res.get("moved"))
                manifest.append({
                    "name": name, "src": src_full, "dst": res.get(
                        "dst") or dst_full,
                    "verified": done and bool(res.get("verified",
                                                      done)),
                    "sha256": res.get("sha256"),
                    "error": None if done else
                    (res.get("detail") or "reported unverified")})
            except Exception as e:  # noqa: BLE001 — item isolation
                manifest.append({"name": name, "src": src_full,
                                 "dst": dst_full, "verified": False,
                                 "error": str(e)[:180]})
            self._emit_progress(mission_id, i + 1, total, name,
                                manifest[-1])
        return manifest

    def _emit_progress(self, mission_id, i, total, name, entry) -> None:
        self._emit(mission_id, {"step": f"{i}/{total}", "name": name,
                                "verified": entry.get("verified"),
                                "error": entry.get("error")})

    def _hop_phone_to_laptop(self, mission_id, phone_dev, laptop_dev,
                             dst_dir, items) -> list[dict]:
        """Phone -> relay -> pendrive. Phase A: the mission waits for
        the operator to share from the phone app; when relay records
        whose recipient is the laptop appear and finish (digest-
        enforced by the relay), phase B stages/copy them onto the
        drive with a re-verification. If nothing matches within the
        wait window the mission honestly says so."""
        if not laptop_dev:
            self._fail(mission_id, "laptop destination not online")
            return []
        wanted = [os.path.basename(str(i).strip()) for i in items]
        deadline = self._now() + self._wait_s
        seen: dict[str, str] = {}
        manifest: list[dict] = []
        stage_note = ""
        while self._now() < deadline:
            if self._cancelled(mission_id):
                return [{"name": w, "verified": False,
                         "error": "cancelled", "cancelled": True}
                        for w in wanted]
            try:
                for rec in self._xfer.list():
                    if rec.state != "succeeded":
                        continue
                    if rec.source_device != phone_dev.device_id:
                        continue
                    if rec.dest_device != laptop_dev.device_id:
                        continue
                    if rec.filename not in wanted:
                        continue
                    seen[rec.transfer_id] = rec.filename
            except Exception:
                pass
            if len(seen) >= len(wanted):
                break
            time.sleep(min(self._poll_s, max(
                0.05, deadline - self._now())))
            stage_note = "still waiting for the phone to share"
        missing = [w for w in wanted if w not in seen.values()]
        if missing:
            self._enter_wait(
                mission_id,
                reason=f"no completed relay transfer for: "
                       f"{', '.join(missing[:5])[:140]}"
                       + (f" ({stage_note})" if stage_note else ""),
                options=[{"question": "share these files from the "
                          "phone's Zara app share flow, then"
                          " resume the mission",
                          "options": []}])
            # don't hard-fail: the operator shares, then resumes
            return [{"name": w, "verified": False,
                     "error": "not shared by the phone within the "
                              "wait window", "waiting": True}
                    for w in missing]
        # phase B: stage + verified copy onto the drive
        for i, (xfer_id, fname) in enumerate(seen.items()):
            if self._cancelled(mission_id):
                manifest.append({"name": fname, "verified": False,
                                 "error": "cancelled", "cancelled": True})
                continue
            try:
                st = self._execute("storage.xfer_fetch",
                                   {"transfer_id": xfer_id,
                                    "dst_dir": dst_dir},
                                   laptop_dev.device_id) or {}
                manifest.append({
                    "name": fname,
                    "xfer_id": xfer_id,
                    "sha256": st.get("sha256"),
                    "dst": st.get("dst"),
                    "verified": bool(st.get("verified")),
                    "error": None if st.get("verified")
                    else (st.get("error") or st.get("detail") or
                          "unverified")})
            except Exception as e:  # noqa: BLE001
                manifest.append({"name": fname, "verified": False,
                                 "xfer_id": xfer_id,
                                 "error": str(e)[:180]})
            self._emit_progress(mission_id, i + 1, len(seen), fname,
                                manifest[-1])
        extra: list[dict] = []
        return manifest + extra


# the laptop->phone hop is generic and dispatched by name in
# _execute_steps (kept single-file here for review simplicity)
def _hop_laptop_to_phone(runner: "FileTransferMissionRunner",
                         mission_id: str, laptop_dev, phone_dev,
                         src_path: str, items: list, move: bool
                         ) -> list[dict]:
    if move:
        return [{"name": name, "verified": False,
                 "error": "cross-device move is not supported over the "
                          "relay (the source delete cannot be verified "
                          "remotely); use copy", "unsupported": True}
                for name in items]
    if not (laptop_dev and phone_dev):
        return [{"name": i, "verified": False,
                 "error": "source or phone device not online"}
                for i in items]
    runner._mark(mission_id, "hop")
    manifest = []
    for i, name in enumerate(items):
        if runner._cancelled(mission_id):
            manifest.append({"name": name, "verified": False,
                             "error": "cancelled before this item",
                             "cancelled": True})
            continue
        src_full = (src_path.rstrip("/") + "/" +
                    name.strip().lstrip("/")) if src_path else name
        try:
            res = runner._execute("storage.xfer_send", {
                "src": src_full,
                "recipient_device": phone_dev.device_id},
                laptop_dev.device_id) or {}
            if res.get("verified"):
                manifest.append({
                    "name": name, "src": src_full,
                    "relay": res.get("transfer_id"),
                    "sha256": res.get("sha256"), "size": res.get("size"),
                    "verified": True,
                    "fetched": bool(res.get("acked")),
                    "status": res.get("status") or "delivered"})
            else:
                manifest.append({
                    "name": name, "src": src_full,
                    "verified": False,
                    "error": res.get("error") or
                    "agent reported unverified"})
        except Exception as e:  # noqa: BLE001 — item isolation
            manifest.append({"name": name, "src": src_full,
                             "verified": False, "error": str(e)[:180]})
        runner._emit_progress(mission_id, i + 1, len(items), name,
                              manifest[-1])
    return manifest
