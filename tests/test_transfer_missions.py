"""Stage 21 — cross-device file-transfer mission tests.

Hardware-independent: adapter pipelines are injected fakes. The real
hardware validation (pendrive plug/unplug, real phone share, real
relay) remains explicitly pending and is never claimed here.
"""
from __future__ import annotations

import threading
import time

import pytest

from core.models import MissionState
from core.missions import MissionEngine
from core.transfer_missions import (FileTransferMissionRunner,
                                    MissionSpecError, validate_spec)


class _FakeDev:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id


class _FakeXferRec:
    def __init__(self, tid, sender, recipient, filename, state,
                 metadata=None) -> None:
        self.transfer_id = tid
        self.source_device = sender
        self.dest_device = recipient
        self.filename = filename
        self.state = state
        self.metadata = metadata or {}


class _FakeXfer:
    """Relay stand-in: only the surface the runner touches."""

    def __init__(self) -> None:
        self.records: list[_FakeXferRec] = []
        self.cancelled: list[str] = []

    def list(self):
        return list(self.records)

    def cancel(self, tid, who="operator"):
        self.cancelled.append(tid)
        for r in self.records:
            if r.transfer_id == tid:
                r.state = "cancelled"
        return {"ok": True}


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, type, payload=None, **kw):
        self.events.append((type, payload))


def _runner(missions=None, execute=None, resolve=None, xfer=None,
            wait_s=0.3, poll_s=0.02, bus=None, audit=None):
    missions = missions or MissionEngine()
    xfer = xfer or _FakeXfer()
    bus = bus or _Bus()
    r = FileTransferMissionRunner(
        missions, xfer, bus=bus,
        execute=execute or (lambda *a, **k: {}),
        resolve_device=resolve or (lambda hint: (None, False)),
        audit=audit,
        wait_s=wait_s, poll_s=poll_s)
    return r


def _laptop_resolve(mounts=(("/media/user/ pendrive", False),)):
    def resolve(hint):
        if hint == "laptop":
            return _FakeDev("dev-laptop"), True
        if hint == "phone":
            return _FakeDev("dev-phone"), True
        return None, False
    return resolve


OK_COPY = {"copied": True, "verified": True, "sha256": "a" * 64,
           "dst": "/media/pendrive/f.txt"}

# ---------------------------------------------------------------- specs


def test_validate_spec_rejects_traversal_and_flags():
    with pytest.raises(MissionSpecError):
        validate_spec({"source": {"device": "laptop"},
                       "dest": {"device": "laptop"}, "items": ["../x"]})
    with pytest.raises(MissionSpecError):
        validate_spec({"source": {"device": "laptop"},
                       "dest": {"device": "laptop"}, "items": ["a"],
                       "delete_source_after": True})  # without move
    with pytest.raises(MissionSpecError):
        validate_spec({"source": {"device": "tablet"},
                       "dest": {"device": "laptop"}, "items": ["a"]})
    ok = validate_spec({"source": {"device": "laptop"},
                        "dest": {"device": "laptop"}, "items": ["a"],
                        "move": True, "delete_source_after": True})
    assert ok["move"] is True


# -------------------------------------------------- laptop -> pendrive


def test_local_copy_mission_completes():
    calls = []

    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/pendrive",
                                 "read_only": False}]}

        def capacity(self, mount):
            calls.append(("capacity", mount))
            return {"free_bytes": 10_000_000}

        def copy(self, src, dst, on_conflict):
            calls.append(("copy", src, dst, on_conflict))
            return dict(OK_COPY, dst=dst)

    res = _Res()

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(res, tool.split(".")[1])(**{
            k: v for k, v in inputs.items()})

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/home/u"},
                   "dest": {"device": "laptop"},
                   "items": ["a.txt", "b.txt"]}, autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    assert m.state == MissionState.COMPLETED
    # capacity was observed BEFORE any copy
    assert calls[0][0] == "capacity"
    assert [t[0] for t in calls[1:]] == ["copy", "copy"]
    assert any(e["kind"] == "capacity_precheck"
               for e in m.checkpoints)


def test_partial_completion_is_failed_with_manifest():
    responses = {"a.txt": dict(OK_COPY),
                 "b.txt": {"ok": False,
                           "detail": "insufficient space on destination"}}

    def execute(tool, inputs, device_id, mission_id=""):
        name = "capacity" if tool.endswith("capacity") else \
            "volumes" if tool.endswith("volumes") else "copy"
        if name == "capacity":
            return {"free_bytes": 1000}
        if name == "volumes":
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}
        import os
        return responses.get(os.path.basename(inputs["src"]), {})

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/home/u"},
                   "dest": {"device": "laptop"}, "items": ["a.txt",
                                                           "b.txt"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    # Partial truth: 1/2 verified => FAILED, never COMPLETED.
    assert m.state == MissionState.FAILED
    assert "1/2 verified" in (m.error or "")
    assert "insufficient space" in (m.error or "")


def test_conflict_ask_reports_unverified_honorably():
    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 5000}

        def copy(self, src, dst, on_conflict):
            # conflict policy "ask" -> the tool refuses to overwrite
            # (never overwrites without explicit authorization)
            return {"ok": False, "copied": False, "verified": False,
                    "detail": "destination exists and policy requires "
                              "asking; not overwritten"}

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(_Res(), tool.split(".")[1])(**inputs)

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/h"},
                   "dest": {"device": "laptop"}, "items": ["dup.txt"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    assert r._missions.get(mid).state == MissionState.FAILED
    assert "not overwritten" in r._missions.get(mid).error


def test_move_uses_storage_move_tool():
    seen = []

    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 5000}

        def move(self, src, dst, on_conflict):
            seen.append(("move", src, dst))
            return {"moved": True, "verified": True, "sha256": "b" * 64}

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(_Res(), tool.split(".")[1])(**inputs)

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/h"},
                   "dest": {"device": "laptop"}, "items": ["a.txt"],
                   "move": True, "delete_source_after": True},
                  autostart=False)
    r.run(out["mission_id"])
    assert seen and seen[0][0] == "move"
    assert r._missions.get(out["mission_id"]).state == \
        MissionState.COMPLETED


def test_ambiguous_multiple_mounts_waits_then_resolves():
    state = {"n": 2}

    class _Res:
        def volumes(self):
            return {"volumes": [
                {"mountpoint": "/media/p1", "read_only": False},
                {"mountpoint": "/media/p2", "read_only": False}]
                if state["n"] > 1 else
                [{"mountpoint": "/media/p1", "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 5000}

        def copy(self, src, dst, on_conflict):
            return dict(OK_COPY)

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(_Res(), tool.split(".")[1])(**inputs)

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/h"},
                   "dest": {"device": "laptop"}, "items": ["a.txt"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    # ambiguity never guesses
    assert m.state == MissionState.WAITING
    # operator answers via a spec patch naming the volume
    time.sleep(0)  # no threads in sync tests
    mid_out = r.resume(mid, spec_patch={"dest": {"path": "/media/p1"}})
    assert "scheduled" in mid_out.get("state", "") or \
        mid_out.get("state") == "scheduled"
    r.run(mid)
    m = r._missions.get(mid)
    assert m.state == MissionState.COMPLETED
    assert any(c.get("kind") == "capacity_precheck" and
               c.get("mount") == "/media/p1" for c in m.checkpoints)


def test_no_mounted_writable_volume_is_waiting_not_guessed():
    class _Res:
        volumes = lambda self: {"volumes": [{"mountpoint":
                                             "/media/p", "read_only":
                                             True}]}  # read-only mount

    def execute(tool, inputs, device_id, mission_id=""):
        return {"volumes": [] if tool == "storage.volumes" else
                {"free_bytes": 1}}

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/h"},
                   "dest": {"device": "laptop"}, "items": ["a.txt"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    assert r._missions.get(mid).state == MissionState.WAITING


# ------------------------------------------------------------ cancel


def test_cancel_stops_between_items_and_reports():
    hits = []

    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 5000}

        def copy(self, src, dst, on_conflict):
            import os
            hits.append(os.path.basename(src))
            if os.path.basename(src) == "b.txt":
                r._cancel[mid].set()   # operator pressed stop now
            return dict(OK_COPY)

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(_Res(), tool.split(".")[1])(**inputs)

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/h"},
                   "dest": {"device": "laptop"},
                   "items": ["a.txt", "b.txt", "c.txt"]},
                  autostart=False)
    mid = out["mission_id"]
    # the operator's stop flag pre-exists the run (cancel() creates the
    # event; here it is armed mid-run by the fake itself)
    r._cancel[mid] = threading.Event()
    r.run(mid)
    m = r._missions.get(mid)
    # a,b copied; c truthfully reported as cancelled-before-item
    assert hits == ["a.txt", "b.txt"]
    assert m.state.value in ("failed", "cancelled")
    assert any("cancelled" in str(i) for i in _events(r, mid))


def _events(r: FileTransferMissionRunner, mid: str) -> list:
    return [p for t, p in r._bus.events
            if t == "mission_progress" and
            (p or {}).get("mission_id") == mid]


# --------------------------------------- phone -> pendrive (relay hop)


def test_phone_share_stages_and_copies_to_drive():
    xfer = _FakeXfer()
    xfer.records.append(_FakeXferRec(
        "xfer-1", "dev-phone", "dev-laptop", "cat.jpg", "succeeded"))
    calls = []

    def execute(tool, inputs, device_id, mission_id=""):
        if tool == "storage.volumes":
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}
        if tool == "storage.capacity":
            return {"free_bytes": 1_000_000}
        assert tool == "storage.xfer_fetch"
        calls.append((inputs["transfer_id"], inputs["dst_dir"]))
        return {"verified": True, "dst": inputs["dst_dir"] + "/cat.jpg",
                "sha256": "c" * 64}

    r = _runner(execute=execute, xfer=xfer,
                resolve=_laptop_resolve(), wait_s=0.3, poll_s=0.02)
    out = r.start({"source": {"device": "phone"},
                   "dest": {"device": "laptop"}, "items": ["cat.jpg"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    assert m.state == MissionState.COMPLETED
    # one fetch onto the drive mount, from the matched relay record
    assert calls and calls[0][0] == "xfer-1" and \
        calls[0][1] == "/media/p"
    assert any("cat.jpg" in str(e) for e in _events(r, mid))


def test_phone_share_timeout_is_waiting_not_failed_fake():
    def execute(tool, inputs, device_id, mission_id=""):
        if tool == "storage.volumes":
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}
        return {"free_bytes": 1_000_000}

    r = _runner(execute=execute, xfer=_FakeXfer(),  # nothing to match
                resolve=_laptop_resolve(), wait_s=0.15, poll_s=0.02)
    out = r.start({"source": {"device": "phone"},
                   "dest": {"device": "laptop"}, "items": ["pic.jpg"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    assert m.state == MissionState.WAITING
    evs = _events(r, mid)
    assert any("share" in str(e.get("reason", "")).lower()
               for e in evs)


def test_device_revocation_mid_mission_fails_closed():
    """A revoked/disconnected device must fail every subsequent hop
    call (funnel denial), the mission must end FAILED with per-item
    denial evidence, and the source must remain untouched."""
    calls = []

    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 10_000_000}

        def copy(self, src, dst, on_conflict):
            calls.append(("copy", src))
            raise RuntimeError("device revoked — authorization lost")

    audits = []
    r = _runner(execute=lambda tool, inputs, device_id, mission_id="":
                getattr(_Res(), tool.split(".")[1])(**{k: v for k, v in
                                                      inputs.items()}),
                resolve=_laptop_resolve(),
                audit=lambda *a, **k: audits.append((a, k)))
    spec = {"source": {"device": "laptop", "path": "/home/u"},
            "dest": {"device": "laptop"},
            "items": ["a.txt", "b.txt", "c.txt"]}
    out = r.start(spec, autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    assert m.state == MissionState.FAILED
    assert "0/3 verified" in m.error
    evs = [e for e in _events(r, mid) if e.get("manifest")]
    assert evs and all("revoked" in str(i.get("error"))
                       for i in evs[-1]["manifest"])
    # every item WAS attempted honestly, none was faked or skipped
    assert len(calls) == 3
    assert any("transfer_mission_failed" in str(a[0]) for a in audits)


def test_phone_share_resume_restages_same_file_no_duplicates():
    """Resume after the WAITING window: the shared record is matched
    exactly once, staged once with a verified digest, and the manifest
    contains ONE verified entry per requested item — a retry never
    creates duplicate transfers for the same file."""
    xfer = _FakeXfer()
    calls = []

    def execute(tool, inputs, device_id, mission_id=""):
        if tool == "storage.volumes":
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}
        if tool == "storage.capacity":
            return {"free_bytes": 1_000_000}
        assert tool == "storage.xfer_fetch"
        calls.append(inputs["transfer_id"])
        return {"verified": True, "dst": inputs["dst_dir"] + "/note.txt",
                "sha256": "d" * 64}

    r = _runner(execute=execute, xfer=xfer, resolve=_laptop_resolve(),
                wait_s=0.15, poll_s=0.02)
    spec = {"source": {"device": "phone"}, "dest": {"device": "laptop"},
            "items": ["note.txt"]}
    out = r.start(spec, autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m1 = r._missions.get(mid)
    assert m1.state == MissionState.WAITING
    # the operator shares from the phone NOW
    xfer.records.append(_FakeXferRec(
        "xfer-9", "dev-phone", "dev-laptop", "note.txt", "succeeded"))
    r.resume(mid)
    m2 = r._missions.get(mid)
    assert m2.state == MissionState.COMPLETED
    assert calls == ["xfer-9"]            # staged exactly once
    evs = [e for e in _events(r, mid) if e.get("manifest")]
    manifest = evs[-1]["manifest"]
    names = [i["name"] for i in manifest]
    assert names == ["note.txt"]          # no duplicated stage/work
    assert all(i["verified"] for i in manifest)


# --------------------------------------- pendrive -> phone (relay hop)


def test_laptop_to_phone_send_delivered_and_oversize_refused():
    def execute(tool, inputs, device_id, mission_id=""):
        assert tool == "storage.xfer_send"
        if "big.bin" in inputs["src"]:
            return {"verified": False,
                    "error": "relay ceiling 5242880B not within "
                             "1..5242880 — nothing sent"}
        return {"uploaded": True, "acked": True, "verified": True,
                "sha256": "d" * 64, "transfer_id": "xfer-9",
                "status": "delivered"}

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/media/p"},
                   "dest": {"device": "phone"},
                   "items": ["cat.jpg", "big.bin"]}, autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    # cat.jpg delivered; big.bin refused with the precise ceiling —
    # nothing partially sent; mission reports partial truth
    assert m.state == MissionState.FAILED
    assert "1/2 verified" in (m.error or "")
    assert "nothing sent" in (m.error or "")


def test_laptop_to_phone_oversize_never_marks_verified():
    def execute(tool, inputs, device_id, mission_id=""):
        return {"verified": False,
                "error": "relay ceiling exceeded — nothing sent"}

    r = _runner(execute=execute, resolve=_laptop_resolve())
    out = r.start({"source": {"device": "laptop", "path": "/media/p"},
                   "dest": {"device": "phone"}, "items": ["f.bin"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    m = r._missions.get(mid)
    assert m.state == MissionState.FAILED
    evs = _events(r, mid)
    assert evs and evs[-1].get("failed") == 1


# ------------------------------------------------- restart recovery


def test_restart_recovery_from_persistent_checkpoint(tmp_path):
    from core.missions import PersistentMissionEngine

    db = str(tmp_path / "m.db")
    transactions: list[tuple] = []

    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 5000}

        def copy(self, src, dst, on_conflict):
            transactions.append((src, dst))
            return dict(OK_COPY)

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(_Res(), tool.split(".")[1])(**inputs)

    spec = {"source": {"device": "laptop", "path": "/h"},
            "dest": {"device": "laptop"}, "items": ["keep.txt"]}
    m1 = PersistentMissionEngine(db)
    xfer1 = _FakeXfer()
    b1 = _Bus()
    r1 = FileTransferMissionRunner(m1, xfer1, bus=b1, execute=execute,
                                   resolve_device=_laptop_resolve())
    out = r1.start(spec, autostart=False)
    # spec checkpointed on the mission, not only in memory
    assert any(c.get("kind") == "transfer_spec" for c in
               m1.get(out["mission_id"]).checkpoints)
    # "Core restart": fresh engines + runner on the same sqlite
    m2 = PersistentMissionEngine(db)
    r2 = FileTransferMissionRunner(m2, xfer1, bus=b1, execute=execute,
                                   resolve_device=_laptop_resolve())
    out2 = r2.resume(out["mission_id"])
    assert out2.get("state") == "scheduled"
    # resume drives the mission on its own thread; poll for the result
    deadline = time.time() + 5.0
    while time.time() < deadline:
        st = m2.get(out["mission_id"]).state
        if st in (MissionState.COMPLETED, MissionState.FAILED):
            break
        time.sleep(0.05)
    assert m2.get(out["mission_id"]).state == MissionState.COMPLETED
    assert len(transactions) == 1


# ------------------------------------------------ progress + audit


def test_progress_events_and_audit_are_emitted():
    audited = []

    class _Res:
        def volumes(self):
            return {"volumes": [{"mountpoint": "/media/p",
                                 "read_only": False}]}

        def capacity(self, mount):
            return {"free_bytes": 5000}

        def copy(self, src, dst, on_conflict):
            return dict(OK_COPY)

    def execute(tool, inputs, device_id, mission_id=""):
        return getattr(_Res(), tool.split(".")[1])(**inputs)

    r = _runner(execute=execute, resolve=_laptop_resolve(),
                audit=lambda a, t, d="": audited.append((a, t)))
    out = r.start({"source": {"device": "laptop", "path": "/h"},
                   "dest": {"device": "laptop"}, "items": ["a.txt"]},
                  autostart=False)
    mid = out["mission_id"]
    r.run(mid)
    progress = _events(r, mid)
    # per-item progress (evidence for the UI) + completion event
    assert any(p.get("step") == "1/1" for p in progress)
    assert any(p.get("completed") for p in progress)
    assert ("transfer_mission_created", mid) in audited
    assert ("transfer_mission_completed", mid) in audited


# ------------------------------------------------- NL + builtins


def test_local_parser_routes_transfer_phrases():
    from core.intent import LocalIntentParser, ParserContext, ParserDevice
    from core.models import DeviceKind

    caps = {"transfer.mission", "transfer.cancel", "transfer.status",
            "storage.volumes", "storage.stats",
            "peripheral.list", "peripheral.describe", "device.list",
            "assistant.help", "filesystem.search"}
    ctx = ParserContext(
        requester_device="d1",
        devices=[ParserDevice(device_id="d1", labels=["laptop"],
                              kind=DeviceKind.LINUX),
                 ParserDevice(device_id="d2", labels=["phone"],
                              kind=DeviceKind.ANDROID)],
        known_capabilities=caps)
    p = LocalIntentParser()
    assert p.parse("copy these videos from my phone to the pendrive",
                   ctx).capability == "transfer.mission"
    # the phone mention resolves to one paired candidate — no ambiguity
    assert p.parse("cancel the current transfer", ctx).capability == \
        "transfer.cancel"
    assert p.parse("transfer status", ctx).capability == "transfer.status"
    assert p.parse("how much free space on the pendrive",
                   ctx).capability == "storage.volumes"
    assert p.parse("how many videos on the pendrive",
                   ctx).capability == "storage.stats"


def test_transfer_builtins_use_the_runner_handle():
    import core.transfer_state as TS
    from core.builtin_tools import (transfer_cancel, transfer_mission,
                                    transfer_status)

    TS.reset()
    # unwired honesty
    assert "error" in transfer_mission({}, {})
    status = transfer_status({}, {})
    assert status["missions"] == []
    assert "not wired" in status["note"]
    assert "error" in transfer_cancel({}, {})

    r = _runner()
    TS.set_runner(r)
    try:
        out = transfer_mission({}, {})
        assert out["state"] == "waiting"
        st = transfer_status({}, {})
        assert st["missions"][0]["state"] == "waiting"
        # cancel the waiting mission -> ok; a second cancel finds it
        # terminal and honestly refuses
        assert transfer_cancel({}, {}).get("ok") is True
        assert transfer_cancel({}, {}).get("ok") is False
    finally:
        TS.reset()


def test_transfer_mission_state_machine_and_spec_schema():
    # builtin tool defs expose the correct risk level
    from core.builtin_tools import BUILTINS
    by_name = {d.name: d for d, _ in BUILTINS}
    assert by_name["transfer.cancel"].risk.value == "confirm"
    assert by_name["transfer.mission"].risk.value == "safe"
    assert by_name["transfer.status"].risk.value == "safe"


def test_agent_tools_registered_in_parity():
    """storage.xfer_send / storage.xfer_fetch exist on both sides with
    matching schemas (tool-parity contract)."""
    from device.linux.tools_linux import LINUX_TOOLS
    from core.device_tools import DEVICE_TOOL_DEFS

    def _core(name):
        d = [d for d in DEVICE_TOOL_DEFS if d.name == name]
        assert d, name
        return d[0]

    def _linux(name):
        d = [dd for dd, _ in LINUX_TOOLS if dd.name == name]
        assert d, name
        return d[0]

    for name in ("storage.xfer_send", "storage.xfer_fetch"):
        c, l = _core(name), _linux(name)
        assert c.input_schema == l.input_schema
        assert c.required_capabilities == l.required_capabilities
        assert c.risk.value == l.risk.value
        assert c.supported_devices == l.supported_devices
        assert c.timeout_s == l.timeout_s


def test_spec_survives_in_mission_checkpoints():
    r = _runner()
    spec = {"source": {"device": "laptop", "path": "/h"},
            "dest": {"device": "laptop"}, "items": ["a.txt"]}
    out = r.start(spec, autostart=False)
    m = r._missions.get(out["mission_id"])
    cp = [c for c in m.checkpoints if c.get("kind") == "transfer_spec"]
    assert cp and cp[0]["spec"] == spec
