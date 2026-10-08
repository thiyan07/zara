"""Stage 15 tests: real byte movement through the Device Fabric.

Unit tests build TransferEngine directly; API tests use the REAL FastAPI
stack (TestClient). No hardware involved; nothing faked as physical.
"""
import base64
import hashlib
import os
import time

import pytest
from fastapi.testclient import TestClient

from core.app import build_stack, create_app
from core.models import DeviceKind, DeviceState
from core.transfer import (MAX_CHUNK_BYTES, MAX_TRANSFER_BYTES,
                           TransferEngine, TransferRejected, TransferStore,
                           sanitize_filename)

OP = {"Authorization": "Bearer dev-token"}


def make_stack(fabric_db="", transfer_dir=""):
    import tempfile as _tf
    if not transfer_dir:
        transfer_dir = _tf.mkdtemp(prefix="zara-xfer-test-")
    os.environ["ZARA_TRANSFER_DIR"] = transfer_dir
    try:
        return build_stack(fabric_db=fabric_db)
    finally:
        os.environ.pop("ZARA_TRANSFER_DIR", None)


def make_engine(stack, tmp_path):
    root = str(tmp_path / "xfer")
    return TransferEngine(stack["fabric"], TransferStore(root)), root


def pair(stack, device_id, kind="linux", battery=80.0, charging=True):
    code = stack["device_auth"].enroll(device_id, DeviceKind(kind))
    stack["device_auth"].claim(code)
    stack["devices"].register(DeviceState(
        device_id=device_id, kind=DeviceKind(kind), capabilities=[],
        online=True, status="online"))
    d = stack["devices"].get(device_id)
    d.battery_pct = battery
    d.charging = charging
    return device_id


def grant_for(stack, sender, caps=None, ttl_s=600):
    return stack["fabric"].create_grant(
        sender, caps or ["files.transfer"], "confirm",
        ttl_s=ttl_s).grant_id


def payload(n, seed=7):
    return bytes((seed + i) % 256 for i in range(n))


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def request_ok(engine, sender="lap-1", recip="vivo-1", data=None,
               name="note.txt", grant=None, stack=None):
    data = payload(1000) if data is None else data
    kw = {} if grant is None else {"grant_id": grant}
    if grant is None and stack is not None:
        kw["grant_id"] = grant_for(stack, sender)
    return engine.request(sender, recip, name, len(data), sha(data),
                          "text/plain", metadata={}, **kw), data


def upload(engine, rec, data, chunk=MAX_CHUNK_BYTES):
    seq = 0
    for i in range(0, len(data), chunk):
        rec = engine.post_chunk(rec.transfer_id, rec.source_device, seq,
                                data[i:i + chunk])
        seq += 1
    return rec


def ready(stack, tmp_path, sender="lap-1", recip="vivo-1"):
    pair(stack, sender, "linux")
    pair(stack, recip, "android")
    engine, root = make_engine(stack, tmp_path)
    return engine, root, grant_for(stack, sender)


# ---------- filename contract ----------

@pytest.mark.parametrize("bad", [
    "../escape.txt", "../../e.txt", "/tmp/e.txt", "/etc/x", "..\\e.txt",
    "", ".", "..", ".hidden", "a/b", "a\\b", "$HOME/x", "`id`", "~/x",
    "nul\x00byte", "lead ", " trail", "semi;colon", "quot\"e",
    "x" * 129, "ünïcode.txt", "emoji😀.txt", "star*.txt",
])
def test_sanitize_rejects(bad):
    with pytest.raises(TransferRejected):
        sanitize_filename(bad)


@pytest.mark.parametrize("good", ["a.txt", "note-1_final.TXT", "x",
                                  "report.2026-10-04.bin"])
def test_sanitize_accepts(good):
    assert sanitize_filename(good) == good


def test_sanitize_non_text():
    with pytest.raises(TransferRejected):
        sanitize_filename(None)
    with pytest.raises(TransferRejected):
        sanitize_filename(123)


# ---------- request validation ----------

def test_request_contract_errors(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    with pytest.raises(TransferRejected):  # bad sha
        engine.request("lap-1", "vivo-1", "a.txt", 10, "zzz", "",
                       gid)
    with pytest.raises(TransferRejected):  # bad size
        engine.request("lap-1", "vivo-1", "a.txt", 0, sha(b"x"), "", gid)
    with pytest.raises(TransferRejected):  # oversize
        engine.request("lap-1", "vivo-1", "a.txt", MAX_TRANSFER_BYTES + 1,
                       sha(b"x"), "", gid)
    with pytest.raises(TransferRejected):  # self transfer
        engine.request("lap-1", "lap-1", "a.txt", 10, sha(b"0123456789"),
                       "", gid)
    with pytest.raises(TransferRejected):  # bad content type
        engine.request("lap-1", "vivo-1", "a.txt", 10, sha(b"0123456789"),
                       "not a type!", gid)
    with pytest.raises(TransferRejected):  # metadata too large
        engine.request("lap-1", "vivo-1", "a.txt", 10, sha(b"0123456789"),
                       "", gid, metadata={"blob": "x" * 3000})


def test_request_unknown_and_revoked(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    with pytest.raises(TransferRejected):  # unknown sender
        engine.request("ghost", "vivo-1", "a.txt", 10, sha(b"0123456789"),
                       "", gid)
    with pytest.raises(TransferRejected):  # unknown recipient
        engine.request("lap-1", "ghost", "a.txt", 10, sha(b"0123456789"),
                       "", gid)
    stack["fabric"].revoke_device("vivo-1")
    with pytest.raises(TransferRejected):
        engine.request("lap-1", "vivo-1", "a.txt", 10, sha(b"0123456789"),
                       "", gid)


def test_request_offline_recipient(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    stack["devices"].mark_offline("vivo-1")
    with pytest.raises(TransferRejected) as e:
        engine.request("lap-1", "vivo-1", "a.txt", 10, sha(b"0123456789"),
                       "", gid)
    assert "offline" in str(e.value)


def test_request_grant_gating(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    d = payload(50)
    with pytest.raises(TransferRejected) as e:  # no grant
        engine.request("lap-1", "vivo-1", "a.txt", len(d), sha(d))
    assert "grant" in str(e.value)
    with pytest.raises(TransferRejected):  # wrong-op grant
        engine.request("lap-1", "vivo-1", "a.txt", len(d), sha(d), "",
                       grant_for(stack, "lap-1", ["system.battery"]))
    pair(stack, "other-1", "linux")
    other_grant = grant_for(stack, "other-1")
    with pytest.raises(TransferRejected):  # another device's grant
        engine.request("lap-1", "vivo-1", "a.txt", len(d), sha(d), "",
                       other_grant)
    exp = grant_for(stack, "lap-1", ttl_s=1)
    time.sleep(1.2)
    with pytest.raises(TransferRejected):  # expired grant
        engine.request("lap-1", "vivo-1", "a.txt", len(d), sha(d), "",
                       exp)
    g = grant_for(stack, "lap-1")
    stack["fabric"].revoke_grant(g)
    with pytest.raises(TransferRejected):  # revoked grant
        engine.request("lap-1", "vivo-1", "a.txt", len(d), sha(d), "", g)
    # live grant is reusable while live (deterministic, still scoped)
    g2 = grant_for(stack, "lap-1")
    engine.request("lap-1", "vivo-1", "a.txt", len(d), sha(d), "", g2)
    engine.request("lap-1", "vivo-1", "b.txt", len(d), sha(d), "", g2)


def test_request_policy_hard_deny(tmp_path):
    stack = make_stack()
    pair(stack, "lap-1", "linux")
    pair(stack, "evil .ssh/id_rsa", "linux")
    engine, _ = make_engine(stack, tmp_path)
    gid = grant_for(stack, "lap-1")
    with pytest.raises(TransferRejected) as e:
        engine.request("lap-1", "evil .ssh/id_rsa", "a.txt", 10,
                       sha(b"0123456789"), "", gid)
    assert "policy" in str(e.value)


def test_request_governor_defer(tmp_path):
    stack = make_stack()
    pair(stack, "lap-1", "linux", battery=5.0, charging=False)
    pair(stack, "vivo-1", "android")
    engine, _ = make_engine(stack, tmp_path)
    with pytest.raises(TransferRejected) as e:
        engine.request("lap-1", "vivo-1", "a.txt", 100, sha(b"x" * 100),
                       "", grant_for(stack, "lap-1"))
    assert "governor" in str(e.value)


def test_request_concurrency_cap(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    d = payload(20)
    for i in range(4):
        engine.request("lap-1", "vivo-1", f"f{i}.txt", len(d), sha(d),
                       "", gid)
    with pytest.raises(TransferRejected) as e:
        engine.request("lap-1", "vivo-1", "f9.txt", len(d), sha(d), "",
                       gid)
    assert "concurrent" in str(e.value)


# ---------- chunk motion + integrity ----------

def test_happy_path_multichunk(tmp_path):
    stack = make_stack()
    engine, root, gid = ready(stack, tmp_path)
    data = payload(200 * 1024)
    rec, _ = request_ok(engine, data=data, stack=stack)
    assert rec.state == "authorized"
    rec = upload(engine, rec, data)
    assert rec.state == "succeeded"
    assert rec.received_bytes == len(data)
    assert os.path.basename(root) == "xfer"
    names = os.listdir(root)
    assert names == [rec.transfer_id + ".bin"]  # client name never on disk


def test_read_windowed(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    data = payload(200 * 1024)
    rec, _ = request_ok(engine, data=data, stack=stack)
    rec = upload(engine, rec, data)
    out = b""
    for off in range(0, len(data), MAX_CHUNK_BYTES):
        blk, _ = engine.read(rec.transfer_id, "vivo-1", off,
                             min(MAX_CHUNK_BYTES, len(data) - off))
        out += blk
    assert out == data
    assert sha(out) == rec.sha256


def test_seq_strictness(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    data = payload(100)
    rec, _ = request_ok(engine, data=data, stack=stack)
    with pytest.raises(TransferRejected):  # seq 1 before seq 0
        engine.post_chunk(rec.transfer_id, "lap-1", 1, data)
    rec = engine.post_chunk(rec.transfer_id, "lap-1", 0, data)
    assert rec.state == "succeeded"
    with pytest.raises(TransferRejected):  # replay after terminal
        engine.post_chunk(rec.transfer_id, "lap-1", 1, data)


def test_chunk_shape_errors(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    data = payload(10)
    rec, _ = request_ok(engine, data=data, stack=stack)
    with pytest.raises(TransferRejected):
        engine.post_chunk(rec.transfer_id, "lap-1", 0, b"")
    with pytest.raises(TransferRejected):
        engine.post_chunk(rec.transfer_id, "lap-1", 0,
                          b"x" * (MAX_CHUNK_BYTES + 1))
    with pytest.raises(TransferRejected):  # overrun declared size
        engine.post_chunk(rec.transfer_id, "lap-1", 0, payload(11))
    with pytest.raises(TransferRejected):  # wrong sender
        engine.post_chunk(rec.transfer_id, "vivo-1", 0, data)
    with pytest.raises(KeyError):  # unknown transfer id
        engine.post_chunk("xfer-000000000000", "lap-1", 0, data)


def test_corruption_fails(tmp_path):
    stack = make_stack()
    engine, root, gid = ready(stack, tmp_path)
    data = bytearray(payload(100 * 1024))
    rec, _ = request_ok(engine, data=bytes(data), stack=stack)
    n = MAX_CHUNK_BYTES
    engine.post_chunk(rec.transfer_id, "lap-1", 0, bytes(data[:n]))
    bad = bytearray(data[n:n + 1000])
    bad[500] ^= 0xFF  # deterministic single-byte corruption
    engine.post_chunk(rec.transfer_id, "lap-1", 1, bytes(bad))
    rec = engine.post_chunk(rec.transfer_id, "lap-1", 2,
                            bytes(data[n + 1000:]))
    assert rec.state == "failed"
    assert "integrity" in rec.error.lower() or "mismatch" in rec.error
    assert os.listdir(root) == []  # no trusted file left behind
    with pytest.raises(TransferRejected):
        engine.read(rec.transfer_id, "vivo-1", 0, 10)


# ---------- cancel / timeout ----------

def test_cancel_semantics(tmp_path):
    stack = make_stack()
    engine, root, gid = ready(stack, tmp_path)
    data = payload(200 * 1024)
    rec, _ = request_ok(engine, data=data, stack=stack)
    engine.post_chunk(rec.transfer_id, "lap-1", 0, data[:MAX_CHUNK_BYTES])
    rec = engine.cancel(rec.transfer_id, "device:lap-1")
    assert rec.state == "cancelled"
    assert os.listdir(root) == []  # partial staging cleaned
    with pytest.raises(TransferRejected):  # chunks after cancel
        engine.post_chunk(rec.transfer_id, "lap-1", 1,
                          data[MAX_CHUNK_BYTES:2 * MAX_CHUNK_BYTES])
    with pytest.raises(TransferRejected):  # cancel is terminal
        engine.cancel(rec.transfer_id)


def test_timeout_expiry(tmp_path):
    stack = make_stack()
    engine, root, gid = ready(stack, tmp_path)
    rec, _ = request_ok(engine, stack=stack)
    rec.expires_at = "2000-01-01T00:00:00"
    out = engine.sweep()
    assert out == [rec.transfer_id]
    assert engine.get(rec.transfer_id).state == "expired"
    assert os.listdir(root) == []


def test_stall_expiry(tmp_path):
    stack = make_stack()
    engine, root, gid = ready(stack, tmp_path)
    data = payload(200 * 1024)
    rec, _ = request_ok(engine, data=data, stack=stack)
    engine.post_chunk(rec.transfer_id, "lap-1", 0, data[:MAX_CHUNK_BYTES])
    assert engine.get(rec.transfer_id).state == "running"
    rec.updated_at = "2000-01-01T00:00:00"  # simulate 25y stall
    out = engine.sweep()
    assert out == [rec.transfer_id]
    r = engine.get(rec.transfer_id)
    assert r.state == "expired" and "stalled" in r.error
    assert os.listdir(root) == []


# ---------- restart ----------

def test_restart_completed_survives(tmp_path):
    db = str(tmp_path / "fabric.db")
    xd = str(tmp_path / "xfer")
    s1 = make_stack(fabric_db=db, transfer_dir=xd)
    pair(s1, "lap-1", "linux")
    pair(s1, "vivo-1", "android")
    e1 = s1["xfer"]  # stack engine: same sqlite + same staging dir
    data = payload(5000)
    rec, _ = request_ok(e1, data=data, stack=s1)
    upload(e1, rec, data)
    s2 = make_stack(fabric_db=db, transfer_dir=xd)
    e2 = s2["xfer"]  # rehydrate ran at build with the same staging dir
    r2 = e2.get(rec.transfer_id)
    assert r2.state == "succeeded" and r2.sha256 == sha(data)
    # Core restart loses in-memory device credentials, so the recipient
    # re-pairs (real recovery procedure) before downloading proof.
    pair(s2, "lap-1", "linux")
    pair(s2, "vivo-1", "android")
    out = b""
    for off in range(0, len(data), MAX_CHUNK_BYTES):
        blk, _ = e2.read(rec.transfer_id, "vivo-1", off,
                         min(MAX_CHUNK_BYTES, len(data) - off))
        out += blk
    assert out == data


def test_restart_inflight_expires(tmp_path):
    db = str(tmp_path / "fabric.db")
    xd = str(tmp_path / "xfer")
    s1 = make_stack(fabric_db=db, transfer_dir=xd)
    pair(s1, "lap-1", "linux")
    pair(s1, "vivo-1", "android")
    e1 = s1["xfer"]
    rec, data = request_ok(e1, data=payload(200 * 1024), stack=s1)
    e1.post_chunk(rec.transfer_id, "lap-1", 0, data[:MAX_CHUNK_BYTES])
    s2 = make_stack(fabric_db=db, transfer_dir=xd)
    e2 = s2["xfer"]
    assert e2.get(rec.transfer_id).state == "expired"
    assert os.listdir(xd) == []


def test_restart_missing_bytes_honest(tmp_path):
    db = str(tmp_path / "fabric.db")
    xd = str(tmp_path / "xfer")
    s1 = make_stack(fabric_db=db, transfer_dir=xd)
    pair(s1, "lap-1", "linux")
    pair(s1, "vivo-1", "android")
    e1 = s1["xfer"]
    rec, data = request_ok(e1, data=payload(100), stack=s1)
    upload(e1, rec, data)
    os.unlink(os.path.join(xd, rec.transfer_id + ".bin"))
    s2 = make_stack(fabric_db=db, transfer_dir=xd)
    e2 = s2["xfer"]
    assert e2.get(rec.transfer_id).state == "failed"


# ---------- revocation ----------

def test_revocation_mid_transfer_cancels(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    data = payload(200 * 1024)
    rec, _ = request_ok(engine, data=data, stack=stack)
    engine.post_chunk(rec.transfer_id, "lap-1", 0, data[:MAX_CHUNK_BYTES])
    stack["fabric"].revoke_device("lap-1")  # revoked sender
    with pytest.raises(TransferRejected):
        engine.post_chunk(rec.transfer_id, "lap-1", 1,
                          data[MAX_CHUNK_BYTES:2 * MAX_CHUNK_BYTES])
    assert engine.get(rec.transfer_id).state == "cancelled"
    with pytest.raises(TransferRejected):  # old id cannot be reused
        engine.post_chunk(rec.transfer_id, "lap-1", 1, b"x")


def test_revoked_recipient_cannot_download(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    rec, data = request_ok(engine, data=payload(100), stack=stack)
    upload(engine, rec, data)
    stack["fabric"].revoke_device("vivo-1")
    with pytest.raises(TransferRejected):  # revoked recipient downloads
        engine.read(rec.transfer_id, "vivo-1", 0, 100)
    with pytest.raises(TransferRejected):  # ... and cannot ack either
        engine.ack(rec.transfer_id, "vivo-1", sha(data))


def test_ack_paths(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    rec, data = request_ok(engine, data=payload(100), stack=stack)
    upload(engine, rec, data)
    ok, r = engine.ack(rec.transfer_id, "vivo-1", sha(data))
    assert ok is True and r.recipient_verified is True
    wrong = "0" * 63 + ("1" if sha(data)[-1] != "1" else "2")
    ok, r = engine.ack(rec.transfer_id, "vivo-1", wrong)
    assert ok is False and r.recipient_verified is False
    with pytest.raises(TransferRejected):  # only bound recipient
        engine.ack(rec.transfer_id, "lap-1", sha(data))


def test_read_gates(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    rec, data = request_ok(engine, data=payload(100), stack=stack)
    with pytest.raises(TransferRejected):  # not complete yet
        engine.read(rec.transfer_id, "vivo-1", 0, 10)
    upload(engine, rec, data)
    with pytest.raises(TransferRejected):
        engine.read(rec.transfer_id, "lap-1", 0, 10)  # wrong recipient
    with pytest.raises(TransferRejected):
        engine.read(rec.transfer_id, "vivo-1", -1, 10)
    with pytest.raises(TransferRejected):
        engine.read(rec.transfer_id, "vivo-1", 0, MAX_CHUNK_BYTES + 1)
    with pytest.raises(TransferRejected):
        engine.read(rec.transfer_id, "vivo-1", 95, 10)  # overrun
    with pytest.raises(TransferRejected):
        engine.read(rec.transfer_id, "ghost", 0, 10)


# ---------- pending / prune ----------

def test_pending_for(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    rec, data = request_ok(engine, data=payload(100), stack=stack)
    p = engine.pending_for("lap-1")
    assert [t["transfer_id"] for t in p["to_continue"]] == \
        [rec.transfer_id]
    assert p["to_download"] == []
    upload(engine, rec, data)
    p = engine.pending_for("vivo-1")
    assert [t["transfer_id"] for t in p["to_download"]] == \
        [rec.transfer_id]
    assert p["to_download"][0]["sha256"] == sha(data)


def test_prune_old_terminal(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    rec, _ = request_ok(engine, stack=stack)
    engine.cancel(rec.transfer_id)
    rec.updated_at = "2000-01-01T00:00:00"
    engine.sweep()
    with pytest.raises(KeyError):
        engine.get(rec.transfer_id)


# ---------- audit ----------

def test_audit_trail_no_secrets(tmp_path):
    stack = make_stack()
    engine, _, gid = ready(stack, tmp_path)
    rec, data = request_ok(engine, data=payload(3000), stack=stack)
    upload(engine, rec, data)
    engine.ack(rec.transfer_id, "vivo-1", sha(data))
    rec2, _ = request_ok(engine, data=payload(10), name="nope.txt",
                         stack=stack)
    with pytest.raises(TransferRejected):
        engine.request("ghost", "vivo-1", "a.txt", 10, sha(b"0123456789"),
                       "", gid)
    rows = stack["audit"].query(100)
    actions = {r["action"] for r in rows}
    text = str(rows)
    assert "transfer_authorized" in actions
    assert "transfer_completed" in actions
    assert "transfer_verified" in actions
    assert "transfer_rejected" in actions
    assert "zara-dev-" not in text  # no credential material
    assert payload(3000).hex() not in text  # no raw bytes


# ---------- HTTP API ----------

def api_client(tmp_path):
    import os as _os
    _os.environ["ZARA_TRANSFER_DIR"] = str(tmp_path / "srv-xfer")
    try:
        stack = build_stack()
    finally:
        _os.environ.pop("ZARA_TRANSFER_DIR", None)
    return TestClient(create_app(stack)), stack


def pair_http(client, device_id, kind="linux"):
    r = client.post("/v1/agent/enroll",
                    json={"device_id": device_id, "kind": kind}, headers=OP)
    assert r.status_code == 200, r.text
    code = r.json()["pairing_code"]
    r = client.post("/v1/agent/claim", json={"pairing_code": code})
    key = r.json()["device_key"]
    h = {"X-Device-Id": device_id, "X-Device-Key": key}
    r = client.post("/v1/agent/register",
                    json={"capabilities": [], "kind": kind,
                          "software_version": "t"}, headers=h)
    assert r.status_code == 200, r.text
    # mark online with healthy battery via heartbeat path state
    return h


def test_api_full_loop(tmp_path):
    client, stack = api_client(tmp_path)
    ha = pair_http(client, "lap-1")
    hb = pair_http(client, "vivo-1", "android")
    g = client.post("/v1/fabric/grants",
                    json={"device_id": "lap-1",
                          "capabilities": ["files.transfer"],
                          "max_risk": "confirm", "ttl_s": 600},
                    headers=OP).json()
    data = payload(150 * 1024, seed=42)
    r = client.post("/v1/agent/xfer/request",
                    json={"recipient_device": "vivo-1",
                          "filename": "stage15-laptop-to-vivo.txt",
                          "size_bytes": len(data), "sha256": sha(data),
                          "content_type": "text/plain",
                          "grant_id": g["grant_id"]}, headers=ha)
    assert r.status_code == 200, r.text
    tid = r.json()["transfer_id"]
    # sender identity forced from auth even if body claims otherwise
    assert r.json()["source_device"] == "lap-1"
    seq = 0
    for i in range(0, len(data), MAX_CHUNK_BYTES):
        c = client.post(f"/v1/agent/xfer/{tid}/chunk",
                        json={"seq": seq,
                              "data_base64": base64.b64encode(
                                  data[i:i + MAX_CHUNK_BYTES]).decode()},
                        headers=ha)
        assert c.status_code == 200, c.text
        seq += 1
    assert c.json()["state"] == "succeeded"
    p = client.get("/v1/agent/xfer/pending", headers=hb).json()
    assert [t["transfer_id"] for t in p["to_download"]] == [tid]
    out = b""
    off = 0
    while off < len(data):
        d = client.get(f"/v1/agent/xfer/{tid}/bytes",
                       params={"offset": off,
                               "length": min(MAX_CHUNK_BYTES,
                                             len(data) - off)},
                       headers=hb)
        assert d.status_code == 200, d.text
        out += base64.b64decode(d.json()["data_base64"])
        off += d.json()["length"]
    assert sha(out) == sha(data)
    a = client.post(f"/v1/agent/xfer/{tid}/ack",
                    json={"sha256": sha(out)}, headers=hb)
    assert a.json()["verified"] is True
    # operator visibility
    g_one = client.get(f"/v1/fabric/xfer/{tid}", headers=OP)
    assert g_one.json()["state"] == "succeeded"
    assert client.get("/v1/agent/xfer/pending",
                      headers=ha).status_code == 200


def test_api_auth_and_gates(tmp_path):
    client, stack = api_client(tmp_path)
    ha = pair_http(client, "lap-1")
    pair_http(client, "vivo-1", "android")
    assert client.post("/v1/agent/xfer/request",
                       json={}).status_code in (401, 403)
    assert client.get("/v1/agent/xfer/pending").status_code in (401, 403)
    g = client.post("/v1/fabric/grants",
                    json={"device_id": "lap-1",
                          "capabilities": ["files.transfer"],
                          "max_risk": "confirm", "ttl_s": 600},
                    headers=OP).json()["grant_id"]
    data = payload(100)
    tid = client.post("/v1/agent/xfer/request",
                      json={"recipient_device": "vivo-1",
                            "filename": "a.txt", "size_bytes": len(data),
                            "sha256": sha(data), "grant_id": g},
                      headers=ha).json()["transfer_id"]
    hb = {"X-Device-Id": "vivo-1", "X-Device-Key": "wrong-key"}
    c = client.post(f"/v1/agent/xfer/{tid}/chunk",
                    json={"seq": 0,
                          "data_base64": base64.b64encode(data).decode()},
                    headers=hb)
    assert c.status_code in (401, 403)  # bad key never moves bytes
    # traversal refused before any write
    r = client.post("/v1/agent/xfer/request",
                    json={"recipient_device": "vivo-1",
                          "filename": "../escape.txt", "size_bytes": 10,
                          "sha256": sha(b"0123456789"), "grant_id": g},
                    headers=ha)
    assert r.status_code == 400
    # oversized chunk refused
    r = client.post(f"/v1/agent/xfer/{tid}/chunk",
                    json={"seq": 0,
                          "data_base64": base64.b64encode(
                              b"y" * (MAX_CHUNK_BYTES + 1)).decode()},
                    headers=ha)
    assert r.status_code == 400
    # operator cancel + terminal replay
    assert client.post(f"/v1/fabric/xfer/{tid}/cancel",
                       headers=OP).status_code == 200
    r = client.post(f"/v1/agent/xfer/{tid}/chunk",
                    json={"seq": 0,
                          "data_base64": base64.b64encode(data).decode()},
                    headers=ha)
    assert r.status_code == 409


def test_api_revoke_blocks_motion(tmp_path):
    client, stack = api_client(tmp_path)
    ha = pair_http(client, "lap-1")
    pair_http(client, "vivo-1", "android")
    g = client.post("/v1/fabric/grants",
                    json={"device_id": "lap-1",
                          "capabilities": ["files.transfer"],
                          "max_risk": "confirm", "ttl_s": 600},
                    headers=OP).json()["grant_id"]
    data = payload(200 * 1024)
    tid = client.post("/v1/agent/xfer/request",
                      json={"recipient_device": "vivo-1",
                            "filename": "big.bin",
                            "size_bytes": len(data), "sha256": sha(data),
                            "grant_id": g}, headers=ha).json()["transfer_id"]
    c = client.post(f"/v1/agent/xfer/{tid}/chunk",
                    json={"seq": 0,
                          "data_base64": base64.b64encode(
                              data[:MAX_CHUNK_BYTES]).decode()},
                    headers=ha)
    assert c.status_code == 200
    assert client.post("/v1/agent/revoke?device_id=lap-1",
                       headers=OP).status_code == 200
    c = client.post(f"/v1/agent/xfer/{tid}/chunk",
                    json={"seq": 1,
                          "data_base64": base64.b64encode(
                              data[MAX_CHUNK_BYTES:2 * MAX_CHUNK_BYTES]
                          ).decode()},
                    headers=ha)
    assert c.status_code in (401, 403, 409)  # identity dead or cancelled
    d = client.get(f"/v1/fabric/xfer/{tid}", headers=OP).json()
    assert d["state"] == "cancelled"
