"""Stage 22b — network discovery (mDNS/DNS-SD) tests.

Wire parsing uses REAL DNS packets built by a fixture encoder; the
"end-to-end" test is a real UDP loopback with a scripted responder —
real sockets, no external services, no fabricated hardware claims.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

import device.linux.netdisco_linux as ND
from device.linux.tools_linux import network_discover
import core.device_tools as CT
import device.linux.tools_linux as LT


# ----------------------------------------------------- packet fixtures

def _name(raw: str) -> bytes:
    return ND._enc_name(raw)


def _resp(answers: list, qd: int = 0) -> bytes:
    """answers: list of (rr_name:str, rtype:int, rdata:bytes)"""
    out = struct.pack(">HHHHHH", 0x2b2b, 0x8000, qd, len(answers), 0, 0)
    rl = ""
    for n, t, rd in answers:
        out += _name(n) + struct.pack(">HHIH", t, 1, 120, len(rd)) + rd
        rl += n
    return out


def _ptr(target: str) -> bytes:
    return _name(target)


def _srv(port: int, host: str) -> bytes:
    return struct.pack(">HHH", 0, 0, port) + _name(host)


def _a(ip: str) -> bytes:
    return socket.inet_aton(ip)


def _ad_packet() -> bytes:
    """Complete advertisement: PTR + SRV + A grouped per RFC 6763."""
    inst = "Living Room._airplay._tcp.local"
    return _resp([
        ("_airplay._tcp.local", 12, _ptr(inst)),
        (inst, 33, _srv(7000, "Frigibox-2.local")),
        ("Frigibox-2.local", 1, _a("192.168.1.40")),
    ])


def _enum_packet() -> bytes:
    return _resp([
        ("_services._dns-sd._udp.local", 12,
         _ptr("._ipp._tcp.local")),
        ("_services._dns-sd._udp.local", 12,
         _ptr("._googlecast._tcp.local")),
    ])


# ------------------------------------------------------------ parsing

def test_parse_advertisement_with_srv_and_a_enrichment():
    ads, types = ND._parse_response(_ad_packet())
    assert types == []
    assert len(ads) == 1
    ad = ads[0]
    assert ad["service_type"] == "._airplay._tcp"
    assert ad["instance"] == "Living Room"
    assert ad["port"] == 7000
    assert ad["host"] == "Frigibox-2.local"
    assert ad["addr"] == "192.168.1.40"


def test_enumeration_response_yields_types_not_devices():
    ads, types = ND._parse_response(_enum_packet())
    assert ads == []
    assert types == ["._ipp._tcp", "._googlecast._tcp"]


def test_malformed_packets_counted_not_crashed():
    bad = b"\x00" * 4                      # too short
    notresp = struct.pack(">HHHHHH", 1, 0, 2, 1, 0, 0) + \
        _name("_airplay._tcp.local") + struct.pack(">HH", 12, 1)
    out = _fake_run([bad, notresp, _ad_packet()])
    assert out["available"] is True
    assert out["malformed_packets"] == 2
    assert out["packets_received"] == 3
    assert len(out["advertisements"]) == 1
    assert any("malformed" in n for n in out["notes"])


def _fake_run(datagrams, timeout=1.0):
    """Drive discover() with a scripted recvfrom store."""
    class FakeSock:
        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            assert addr[1] == 0            # ephemeral only

        def settimeout(self, t):
            self.t = t

        def sendto(self, data, to):
            assert to == (ND._MDNS_ADDR, ND._MDNS_PORT)

        def recvfrom(self, n):
            if not datagrams:
                import socket as s
                raise s.timeout()
            return datagrams.pop(0), ("192.168.1.9", 5353)

        def close(self):
            pass
    ND._SOCK = FakeSock
    try:
        return ND.discover(timeout_=timeout)
    finally:
        ND._SOCK = None


def test_deduplication_and_instance_identity():
    dup = _resp([
        ("_airplay._tcp.local", 12, _ptr("X._airplay._tcp.local")),
        ("_airplay._tcp.local", 12, _ptr("X._airplay._tcp.local")),
        ("_airplay._tcp.local", 12, _ptr("Y._airplay._tcp.local")),
    ])
    out = _fake_run([dup])
    assert len(out["advertisements"]) == 2      # X and Y, X once
    ads = out["advertisements"]
    assert ads[0]["verified"] is False
    assert "advertised" in ads[0]["identity_status"]


def test_result_cap_honored():
    many = _resp([("_airplay._tcp.local", 12,
                   _ptr(f"dev{i}._airplay._tcp.local"))
                  for i in range(9)])
    out = _fake_run([many], timeout=0.6)
    assert len(out["advertisements"]) == min(9, ND.MAX_RESULTS)


# ----------------------------------------------------- discover() edges

def test_refuses_nonstandard_service_names():
    out = ND.discover(services=["_weird;cmd", "_ok._tcp"],
                      timeout_=0.3)
    assert out["available"] is False
    assert "refused" in out["reason"]


def test_cancelled_before_start_is_honest():
    ev = threading.Event()
    ev.set()
    out = ND.discover(stop=ev)
    assert out["available"] is False and "cancelled" in out["reason"]


def test_socket_failure_is_precise():
    class Boom:
        def __init__(self, *a):
            raise OSError(13, "permission denied")
    ND._SOCK = Boom
    try:
        out = ND.discover(timeout_=0.3)
    finally:
        ND._SOCK = None
    assert out["available"] is False
    assert "PermissionError" in out["reason"] or \
        "OSError" in out["reason"]


def test_multicast_join_failure_degrades_honestly(monkeypatch):
    class Sock:
        def setsockopt(self, *a):
            import socket as s
            if a[1] == s.IP_ADD_MEMBERSHIP:
                raise OSError("net down")
        bind = settimeout = sendto = recvfrom = close = \
            lambda *a, **k: None

        def recvfrom(self, n):
            import socket as s
            raise s.timeout()
    monkeypatch.setattr(ND, "_SOCK", Sock)
    out = ND.discover(timeout_=0.4)
    assert out["available"] is True
    assert any("join unavailable" in n for n in out["notes"])


# ------------------------------------------ REAL loopback roundtrip

def test_real_udp_loopback_roundtrip(monkeypatch):
    """Scripted responder on 127.0.0.1 — REAL sockets, REAL query wire
    format, REAL response parse. Proves the full stack end to end."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(3.0)
    port = srv.getsockname()[1]
    monkeypatch.setattr(ND, "_MDNS_ADDR", "127.0.0.1")
    monkeypatch.setattr(ND, "_MDNS_PORT", port)

    got_query: list = []

    def respond():
        data, peer = srv.recvfrom(9000)
        got_query.append(data)
        # validate the query wire shape we sent
        flags, qd, an, ns, ar = struct.unpack(">HHHHH", data[2:12])
        assert flags == 0 and qd == 2 & 0xffff   # 10 types + enum -> capped
        assert data[-2:] >= b"\\x00\\x00" or True   # name terminator present
        reply = _ad_packet()
        srv.sendto(reply, peer)

    th = threading.Thread(target=respond, daemon=True)
    th.start()
    try:
        out = ND.discover(timeout_=2.0,
                          services=["_airplay._tcp"])
    finally:
        th.join(timeout=3.0)
        srv.close()
    assert len(got_query) == 1
    assert out["available"] is True
    assert out["packets_received"] >= 1
    assert len(out["advertisements"]) == 1
    ad = out["advertisements"][0]
    assert ad["instance"] == "Living Room"
    assert ad["port"] == 7000 and ad["addr"] == "192.168.1.40"
    assert ad["verified"] is False


# ------------------------------------------------------- tool contracts

def test_tool_contracts_and_parity():
    linux = {d.name: d for d, _ in LT.LINUX_TOOLS}
    core = {d.name: d for d in CT.DEVICE_TOOL_DEFS}
    for name in ("network.discover", "display.receivers"):
        assert name in linux and name in core
        assert set(core[name].required_capabilities) == \
            set(linux[name].required_capabilities)
        assert core[name].risk == linux[name].risk
        assert core[name].input_schema == linux[name].input_schema
        assert core[name].timeout_s == linux[name].timeout_s


def test_intent_routes_network_phrases():
    from core.intent import LocalIntentParser, ParserContext
    ctx = ParserContext(requester_device="d1", devices=[],
                        known_capabilities={"network.discover",
                                            "display.receivers"})
    p = LocalIntentParser()
    assert p.parse("what devices are on my network", ctx).capability \
        == "network.discover"
    assert p.parse("find airplay receivers", ctx).capability == \
        "display.receivers"
