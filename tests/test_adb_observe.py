"""Tests for the Stage-24b ADB observation layer (read-only).

Directly adopted from the 2026-10-20 GitHub research: the Android
computer-use agents (NexusAndroid / droidjig / omni-use) all build their
act loop on an observe step over adb; Zara adopts ONLY the observe half
(per the device-security policy adb stays detection/observation only —
no input injection surface is ever added below).

No real adb is required: subprocess and PATH are faked per test.
"""

import subprocess

import pytest

from device import adb_linux


# ------------------------------------------------------------------
# fixtures: a realistic `adb devices -l` block and a uiautomator dump
# ------------------------------------------------------------------

DEVICES_L = """List of devices attached
ABC123device\tdevice usb:1-2 product:raven model:Pixel_6_Pro device:raven transport_id:5
emulator-5554\toffline




"""


def _ui_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hierarchy rotation="0">'
        '<node index="0" text="" class="android.widget.FrameLayout"'
        ' content-desc="" clickable="false" bounds="[0,0][1080,2400]"/>'
        '<node index="1" text="Settings" class="android.widget.TextView"'
        ' content-desc="Open settings" clickable="true"'
        ' bounds="[40,120][320,200]"/>'
        '<node index="2" text="" class="android.widget.ImageView"'
        ' content-desc="Battery 84 percent" clickable="false"'
        ' bounds="[900,80][1040,180]"/>'
        '</hierarchy>'
    )


class _FakeRun:
    """Stand-in recorder for subprocess.run with a canned result."""

    def __init__(self, *, stdout=b"", returncode=0, stderr=b""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls = []

    def __call__(self, argv, capture_output=True, timeout=None):
        self.calls.append({"argv": argv, "timeout": timeout})

        class R:
            returncode = self.returncode
            stdout = self.stdout
            stderr = self.stderr

        return R()


class _FakeErrRun:
    def __init__(self, *, returncode=1, stderr=b"error: no devices found"):
        self.returncode, self.stderr = returncode, stderr
        self.calls = []

    def __call__(self, argv, capture_output=True, timeout=None):
        self.calls.append({"argv": argv, "timeout": timeout})

        class R:
            pass

        r = R()
        r.returncode = self.returncode
        r.stdout = b""
        r.stderr = self.stderr
        return r


# ------------------------------------------------------------------
# pure parsers
# ------------------------------------------------------------------

def test_parse_devices_inventory():
    out = adb_linux.parse_devices(DEVICES_L)
    assert len(out) == 2
    assert out[0]["serial"] == "ABC123device"
    assert out[0]["state"] == "device"
    assert out[0]["model"] == "Pixel_6_Pro"
    assert out[0]["transport_id"] == "5"
    assert out[1] == {"serial": "emulator-5554", "state": "offline"}


def test_parse_devices_malformed_never_crashes():
    assert adb_linux.parse_devices("") == []
    assert adb_linux.parse_devices("garbage without columns") == []
    huge = "\n".join("s%d device" % i for i in range(100))
    assert len(adb_linux.parse_devices(huge)) == adb_linux._DEV_LINE_MAX


def test_parse_bounds_roundtrip():
    assert adb_linux.parse_bounds("[40,120][320,200]") == \
        ((40, 120), (320, 200))


def test_parse_bounds_rejects_bad_shapes():
    for bad in ("", "nonsense", "[40,120][x,200]", "[320,200][40,120]",
                "[0,0][0,0]", "[1,2][3,4", None):
        assert adb_linux.parse_bounds(bad) is None


def test_ui_nodes_bounded_metadata_only():
    nodes = adb_linux.ui_nodes(_ui_xml())
    assert len(nodes) == 3
    assert nodes[1]["text"] == "Settings"
    assert nodes[1]["clickable"] is True
    assert nodes[1]["center"] == (180, 160)
    assert nodes[2]["desc"] == "Battery 84 percent"
    # every field is capped metadata; no pixel/media payload anywhere
    for n in nodes:
        assert set(n) <= {"text", "desc", "cls", "clickable", "bounds",
                          "center", "error"}


def test_ui_nodes_invalid_xml_reports_error_item():
    out = adb_linux.ui_nodes("this is not xml <")
    assert len(out) == 1 and "error" in out[0]


# ------------------------------------------------------------------
# faked-transport behaviors (honest refusals, bounded calls)
# ------------------------------------------------------------------

def test_adb_absent_is_honest_refusal(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which", lambda _: None)
    check = adb_linux.adb_available()
    assert check["ok"] is False and "not found" in check["reason"]
    devs = adb_linux.list_devices()
    assert devs == {"ok": False, "reason": check["reason"], "devices": [],
                    "stage": "detection"}
    dump = adb_linux.ui_dump()
    assert dump["ok"] is False and dump["stage"] == "detection"


def test_list_devices_bounded_and_parsed(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    fake = _FakeRun(stdout=DEVICES_L.encode())
    monkeypatch.setattr(adb_linux.subprocess, "run", fake)
    out = adb_linux.list_devices()
    assert out["ok"] is True and len(out["devices"]) == 2
    # every call carries the bounded timeout — never unbounded
    assert fake.calls and all(c["timeout"] == adb_linux.ADB_TIMEOUT_S
                              for c in fake.calls)
    argv = fake.calls[0]["argv"]
    assert argv[0] == "adb" and "-l" in argv and "devices" in argv


def test_list_devices_failure_reports_exact_stage(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    fake = _FakeErrRun(returncode=1)
    monkeypatch.setattr(adb_linux.subprocess, "run", fake)
    out = adb_linux.list_devices()
    assert out["ok"] is False and out["stage"] == "adb"
    assert "no devices found" in out["reason"]


def test_ui_dump_uses_serial_and_exec_out(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    fake = _FakeRun(stdout=_ui_xml().encode())
    monkeypatch.setattr(adb_linux.subprocess, "run", fake)
    out = adb_linux.ui_dump(serial="ABC123device")
    assert out["ok"] is True and len(out["items"]) == 3
    argv = fake.calls[0]["argv"]
    assert argv[:4] == ["adb", "-s", "ABC123device", "exec-out"]
    # fast path: compressed first (speed round), same exec-out stream
    assert argv[-3:] == ["dump", "--compressed", "/dev/tty"]
    assert fake.calls[0]["timeout"] == adb_linux.UI_DUMP_TIMEOUT_S


def test_ui_dump_avoids_persistence_on_device(monkeypatch):
    """The dump must stream through exec-out — never a file the phone
    keeps (no /sdcard artifact, no `adb pull`)."""
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    fake = _FakeRun(stdout=_ui_xml().encode())
    monkeypatch.setattr(adb_linux.subprocess, "run", fake)
    adb_linux.ui_dump()
    argv = " ".join(fake.calls[0]["argv"])
    assert "pull" not in argv and "/sdcard" not in argv


def test_ui_dump_garbage_output_is_honest(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.subprocess, "run",
                        _FakeRun(stdout=b"some unrelated output"))
    out = adb_linux.ui_dump()
    assert out["ok"] is False and out["stage"] == "parse"


def test_ui_dump_banner_prefixed_xml_is_stripped(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    banner = b"UI hierarchies dumped to:\n" + _ui_xml().encode()
    monkeypatch.setattr(adb_linux.subprocess, "run", _FakeRun(stdout=banner))
    out = adb_linux.ui_dump()
    assert out["ok"] is True and len(out["items"]) == 3


def test_ui_dump_timeout_is_bounded_spawn_failure(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="adb", timeout=6.0)

    monkeypatch.setattr(adb_linux.subprocess, "run", slow)
    out = adb_linux.ui_dump()
    assert out["ok"] is False and out["stage"] == "spawn"


# ==================================================================
# state verification — the verify half of the observe→act loop
# (android-use-class agents act THROUGH the paired-device protocol;
# verification here is read-only over the dump we already produce)
# ==================================================================


def test_match_one_semantics():
    node = {"text": "Settings", "desc": "Open settings", "cls":
            "android.widget.TextView", "clickable": True}
    assert adb_linux._match_one(node, {"text": "Settings"})
    assert adb_linux._match_one(node, {"text_contains": "Sett",
                                       "clickable": True})
    assert not adb_linux._match_one(node, {"text": "settings"})  # exact
    assert not adb_linux._match_one(node, {"clickable": False})


def test_match_state_satisfied_with_evidence():
    nodes = adb_linux.ui_nodes(_ui_xml())
    out = adb_linux.match_state(
        nodes, [{"text": "Settings", "clickable": True},
                {"desc_contains": "percent"}])
    assert out["satisfied"] is True
    ev = out["per_condition"][0]["evidence"]
    assert ev["text"] == "Settings" and ev["center"] == (180, 160)
    assert out["per_condition"][1]["evidence"]["desc"] == \
        "Battery 84 percent"


def test_match_state_failure_names_the_broken_key():
    nodes = adb_linux.ui_nodes(_ui_xml())
    out = adb_linux.match_state(
        nodes, [{"text": "Settings", "clickable": False}])
    assert out["satisfied"] is False
    assert "clickable=True" in out["per_condition"][0]["reason"]
    out2 = adb_linux.match_state(nodes, [{"text": "Nope"}])
    assert "no observed node matched" in \
        out2["per_condition"][0]["reason"]


def test_expect_validation_is_loud():
    for bad in (None, [], [{}], [42],
                [{"unknown": "key"}], [{"clickable": "yes"}],
                [{"text": "x" * 300}]):
        with pytest.raises(ValueError):
            adb_linux._validate_expect(bad)
    # caps + limit on count
    many = [{"text_contains": "x"} for _ in range(9)]
    with pytest.raises(ValueError, match="too many"):
        adb_linux._validate_expect(many)


def test_verify_ui_satisfied_first_attempt(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.subprocess, "run",
                        _FakeRun(stdout=_ui_xml().encode()))
    out = adb_linux.verify_ui(
        serial=None, expect=[{"text_contains": "Sett",
                              "clickable": True}])
    assert out["ok"] is True and out["satisfied"] is True
    assert out["attempt"] == 1


def test_verify_ui_bounded_retry_then_honest_failure(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.subprocess, "run",
                        _FakeRun(stdout=_ui_xml().encode()))
    monkeypatch.setattr(adb_linux.time, "sleep", lambda _s: None)
    out = adb_linux.verify_ui(
        serial=None, expect=[{"text": "Missing screen value"}],
        max_attempts=3, interval_s=1.5)
    assert out["ok"] is True and out["satisfied"] is False
    assert out["attempt"] == 3


def test_verify_ui_dump_failure_reports_stage(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.subprocess, "run", _FakeErrRun())
    out = adb_linux.verify_ui(serial=None, expect=[{"text": "x"}])
    assert out["ok"] is False and out["stage"] == "adb"


def test_verify_ui_requires_expect_and_caps_retries(monkeypatch):
    with pytest.raises(ValueError):
        adb_linux.verify_ui(expect=None)
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    fake = _FakeRun(stdout=_ui_xml().encode())
    monkeypatch.setattr(adb_linux.subprocess, "run", fake)
    # attempt cap: asking for 99 tries must still be a bounded 3
    adb_linux.verify_ui(expect=[{"text": "absent"}], max_attempts=99)
    seen = len(fake.calls)
    assert seen == adb_linux._VERIFY_ATTEMPT_MAX


# ==================================================================
# speed round — fast-path dump (compressed-first, measured latency)
# ==================================================================

def test_ui_dump_tries_compressed_first(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    fake = _FakeRun(stdout=_ui_xml().encode())
    monkeypatch.setattr(adb_linux.subprocess, "run", fake)
    out = adb_linux.ui_dump(serial="S1")
    assert out["ok"] is True and out["compressed"] is True
    argv = fake.calls[0]["argv"]
    assert "exec-out" in argv and "uiautomator" in argv
    assert "dump" in argv and "--compressed" in argv
    assert argv[-1] == "/dev/tty"
    assert fake.calls[0]["timeout"] == adb_linux.UI_DUMP_TIMEOUT_S
    assert out["attempts"] == ["compressed"]
    assert out["nodes"] == len(out["items"])
    assert 0 <= out["elapsed_ms"] < 5000


def test_ui_dump_falls_back_to_plain_on_rejected_flag(monkeypatch):
    """Older builds reject --compressed at the adb level — one honest
    fallback, still bounded, mode recorded."""
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    calls = []

    class _MixedRun:
        def __call__(self, argv, capture_output=True, timeout=None):
            calls.append({"argv": argv, "timeout": timeout})
            if "--compressed" in argv:
                class R:
                    returncode = 2
                    stdout = b"usage: uiautomator dump [...]"
                    stderr = b"error: unrecognized arguments"
                return R()

            class R2:
                returncode = 0
                stdout = _ui_xml().encode()
                stderr = b""
            return R2()

    monkeypatch.setattr(adb_linux.subprocess, "run", _MixedRun())
    out = adb_linux.ui_dump()
    assert out["ok"] is True and out["compressed"] is False
    assert out["attempts"] == ["compressed", "plain"]
    assert len(calls) == 2
    assert all(c["timeout"] == adb_linux.UI_DUMP_TIMEOUT_S
               for c in calls)


def test_ui_dump_spawn_failure_does_not_spray_retries(monkeypatch):
    """adb spawn/timeout failure: a flag retry cannot help — fail fast
    at the exact stage with honest timing."""
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")

    def dead(*a, **k):
        raise OSError("adb executable could not run")

    monkeypatch.setattr(adb_linux.subprocess, "run", dead)
    out = adb_linux.ui_dump()
    assert out["ok"] is False and out["stage"] == "spawn"
    assert len(out["attempts"]) == 1


def test_ui_dump_parse_failure_reports_mode_and_bytes(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.subprocess, "run",
                        _FakeRun(stdout=b"unrelated output"))
    out = adb_linux.ui_dump()
    assert out["ok"] is False and out["stage"] == "parse"
    assert out["compressed"] is True and "bytes" in out


def test_verify_ui_reports_speed_telemetry(monkeypatch):
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.subprocess, "run",
                        _FakeRun(stdout=_ui_xml().encode()))
    monkeypatch.setattr(adb_linux.time, "sleep", lambda _s: None)
    out = adb_linux.verify_ui(
        serial=None, expect=[{"desc_contains": "percent"},
                             {"clickable": True, "text": "Settings"}])
    assert out["satisfied"] is True
    assert out["total_ms"] >= 0 and out["nodes_observed"] == 3
    assert out["compressed"] is True
    out2 = adb_linux.verify_ui(
        serial=None, expect=[{"text": "never there"}], interval_s=0.0)
    assert out2["satisfied"] is False and "total_ms" in out2


def test_verify_ui_never_reuses_stale_dumps(monkeypatch):
    """Freshness contract: EVERY attempt re-observes the device. A
    dump whose state changed between attempts must be re-read fresh;
    attempt 2's evidence may only come from attempt 2's dump.
    """
    xml_a = ('<?xml version="1.0"?><hierarchy>'
             '<node text="Old screen" content-desc="" class="t"'
             ' clickable="false" bounds="[0,0][10,10]"/></hierarchy>')
    xml_b = ('<?xml version="1.0"?><hierarchy>'
             '<node text="Action applied" content-desc="sent" class="t"'
             ' clickable="true" bounds="[0,0][10,10]"/></hierarchy>')
    monkeypatch.setattr(adb_linux.shutil, "which",
                        lambda _: "/usr/bin/adb")
    monkeypatch.setattr(adb_linux.time, "sleep", lambda _s: None)
    seen = []

    def alternating(argv, capture_output=True, timeout=None):
        seen.append(len(seen))
        out = xml_a if len(seen) == 1 else xml_b

        class R:
            def __init__(self, b):
                self.returncode, self.stdout = 0, b.encode()
        return R(out)

    monkeypatch.setattr(adb_linux.subprocess, "run", alternating)
    out = adb_linux.verify_ui(
        serial=None, expect=[{"text_contains": "Action applied"}],
        max_attempts=2, interval_s=0.0)
    assert out["satisfied"] is True and out["attempt"] == 2
    assert out["per_condition"][0]["evidence"]["text"] == "Action applied"
    assert out["per_condition"][0]["evidence"]["desc"] == "sent"
    # two attempts → exactly two fresh dumps, never one reused observation
    assert len(seen) == 2
