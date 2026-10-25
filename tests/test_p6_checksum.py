"""P5 feature-hunt ㉒ (2026-10-24) — "sha256 of my file" (fs.checksum).

A one-file digest utility. Contract pinned here:
- the DIGEST is the output — file content never leaves the laptop
  (the house read-only posture: hashing discloses nothing but the
  fingerprint);
- digest algorithms are sha256 (default), sha512, sha1, md5 — weak
  ones (sha1/md5) run ONLY when named explicitly; anything else is
  an honest ValueError naming the supported set;
- the prefix cap (512 MB) makes over-cap digests honest PREFIX
  digests: `stopped` is set, the detail says compare-unsafety, and
  bytes_hashed lets nobody mistake a prefix for a whole file;
- path scope is _fs_path (allowed roots, sensitive refusal), like
  every filesystem surface;
- chat matcher: digest word + a path token (quoted or ~/-/…);
  case is preserved (paths are captured from the RAW utterance,
  never the lowercased one); a valueless path ask clarifies with
  the exact example; absent capability falls through;
- the formatter renders algo, path, digest, bytes; a prefix digest
  is labelled NOT compare-safe;
- timing invariant: the device def waits > 60 s (test_T0).
"""
import hashlib

import pytest

import sys
import os
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

from device.linux.tools_linux import (  # noqa: E402
    hash_file_prefix, fs_checksum)


def test_full_digest_matches_hashlib(tmp_path):
    p = tmp_path / "data.bin"
    payload = b"zara-checksum-fixture" * 1000
    p.write_bytes(payload)
    out = hash_file_prefix(str(p), "sha256", size_cap=64 * 1024 * 1024)
    assert out["digest"] == hashlib.sha256(payload).hexdigest()
    assert out["bytes_hashed"] == len(payload)
    assert out["stopped"] == ""


def test_prefix_cap_tells_the_truth(tmp_path):
    p = tmp_path / "big.bin"
    payload = b"\xa5" * (100 * 1024)
    p.write_bytes(payload)
    out = hash_file_prefix(str(p), "sha256", size_cap=32 * 1024)
    assert out["stopped"] == "size cap"
    assert out["bytes_hashed"] == 32 * 1024      # a *whole* blocks-stopped
    # equals the digest of exactly the first cap bytes
    assert out["digest"] == hashlib.sha256(
        payload[:32 * 1024]).hexdigest()


@pytest.mark.parametrize("algo", ["sha512", "sha1", "md5"])
def test_named_algorithms(tmp_path, algo):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    out = hash_file_prefix(str(p), algo, size_cap=1024 ** 3)
    assert out["digest"] == hashlib.new(algo, b"abc").hexdigest()


def test_tool_refusals(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"x")
    with pytest.raises(ValueError) as e:
        fs_checksum({"path": str(p), "algo": "sha3"}, {})
    assert "supported digest" in str(e.value)
    with pytest.raises(ValueError) as e:
        fs_checksum({"path": str(tmp_path), "algo": "sha256"}, {})
    assert "cannot read" in str(e.value) or \
        "could not read bytes" in str(e.value)


def test_scope_refusals():
    with pytest.raises(PermissionError):
        fs_checksum({"path": "~/.ssh"}, {})
    with pytest.raises(PermissionError):
        fs_checksum({"path": "/usr/share"}, {})
    # NOTE deliberately NOT asserted: the devauth state DB lives in
    # ~/.local/state/zara and is intentionally outside SENSITIVE_HINTS
    # (the pairing/consent surfaces read it); hashing it is harmless
    # (digest only).


def test_registry_and_parity():
    from core.device_tools import DEVICE_TOOL_DEFS
    from device.linux import tools_linux as LT
    core = next(x for x in DEVICE_TOOL_DEFS if x.name == "fs.checksum")
    assert core.timeout_s > 60.0
    assert "filesystem.read" in core.required_capabilities
    assert core.risk.value == "safe"
    assert core.permission.value == "restricted"
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}["fs.checksum"]
    assert set(core.required_capabilities) == \
        set(linux.required_capabilities)
    assert core.input_schema == linux.input_schema
    assert core.risk == linux.risk


CAPS = {"fs.checksum", "storage.find", "storage.scan"}


def _parser():
    from core.intent import LocalIntentParser
    return LocalIntentParser()


def _ctx(caps=CAPS):
    from core.intent import ParserContext, ParserDevice
    return ParserContext(
        known_capabilities=set(caps),
        devices=[ParserDevice(device_id="laptop-1", kind="linux")])


def test_matcher_shapes_and_case_preserved():
    p = _parser()
    out = p.parse("sha256 of ~/Downloads/big.ISO", _ctx())
    assert out.capability == "fs.checksum"
    assert out.parameters["path"] == "~/Downloads/big.ISO"
    assert "algo" not in out.parameters
    out = p.parse("sha256 of '/home/me/My File.zip'", _ctx())
    assert out.parameters["path"] == "/home/me/My File.zip"
    out = p.parse("md5 of ~/x", _ctx())
    assert out.parameters["algo"] == "md5"


def test_matcher_valueless_path_clarifies():
    p = _parser()
    out = p.parse("checksum of big.iso", _ctx())
    assert out.capability == "" and out.needs_clarification
    assert "checksum of" in out.clarification_reason


def test_matcher_absent_capability_falls_through():
    p = _parser()
    out = p.parse("sha256 of ~/x.iso",
                  _ctx(caps={"storage.find"}))
    assert out.capability != "fs.checksum"


def _fmt(result):
    from core.intent import IntentTurnService, IntentProposal
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="fs.checksum", parameters={},
                          confidence=0.8, needs_clarification=False,
                          source="local")
    return IntentTurnService._success_message(
        prop, {"result": result, "stage": "succeeded"})


def test_formatter_full_and_prefix():
    msg = _fmt({"path": "/home/me/a.iso", "algo": "sha256",
                "digest": hashlib.sha256(b"q").hexdigest(),
                "bytes_hashed": 4.4 * 10 ** 9, "stopped": "",
                "detail": "full file (4400000000 bytes hashed)"})
    assert msg.startswith("SHA256 of /home/me/a.iso: ")
    assert "(4.4 GB hashed)" in msg
    msg = _fmt({"path": "/home/me/small.txt", "algo": "sha256",
                "digest": hashlib.sha256(b"s").hexdigest(),
                "bytes_hashed": 9700, "stopped": "",
                "detail": "full file (9700 bytes hashed)"})
    assert "(9.7 KB hashed)" in msg and "0.0 MB" not in msg
    msg = _fmt({"path": "/home/me/huge.vdi", "algo": "sha256",
                "digest": hashlib.sha256(b"w").hexdigest(),
                "bytes_hashed": 512 * 1024 * 1024,
                "stopped": "size cap", "detail": "PREFIX digest — …"})
    assert "(prefix)" in msg
    assert "NOT a compare-safe full digest" in msg
    assert "512 MB" in msg


def test_device_failure_honesty():
    """Live-caught (batch 22): a device tool's ValueError is the REAL
    explanation — the turn must surface it, never a bare 'failed'
    (the failed stage carries the truth in `error`)."""
    from core.app import build_stack
    from core.intent import (IntentTurnService, IntentProposal,
                             LocalIntentParser)
    stack = build_stack()
    svc = IntentTurnService(
        stack["fabric"], stack["resolver"], LocalIntentParser(),
        stack["audit"], stack["missions"], None, None, None, None,
        None, None, None)

    def fake_exec(tool, inputs, device_id, who):
        return {"stage": "failed",
                "error": "cannot read x.bin: [Errno 2] No such file"}
    svc.fabric.execute_on_device = fake_exec
    prop = IntentProposal(request_id="t", utterance="x",
                          capability="fs.checksum",
                          parameters={"path": "~/x.bin"}, confidence=0.8,
                          needs_clarification=False, source="local")
    resp = svc._maybe_execute(prop, {"device_id": "laptop-1"}, "user")
    assert resp["executed"] is False
    assert "cannot read x.bin" in resp["message"], resp
    assert "unexpected failure" not in resp["message"]
    # generic stage message still renders
    svc.fabric.execute_on_device = (
        lambda t, i, d, w: {"stage": "failed", "reason": "denied"})
    resp = svc._maybe_execute(prop, {"device_id": "laptop-1"}, "user")
    assert resp["message"] == "I couldn't do that: denied"
