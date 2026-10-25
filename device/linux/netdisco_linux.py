"""Stage 22b — bounded mDNS/DNS-SD network-device discovery.

STRICT SCOPE (what this is and is NOT):
- Only the mDNS standard mechanism (RFC 6762/6763): one bounded
  multicast query for PTR records of a bounded service-type allowlist
  plus the documented `_services._dns-sd._udp.local` enumeration
  query, listening at most ``timeout_`` seconds. NO port scanning, NO
  subnet sweeps, NO credential probing, NO exploitation — a query for
  ADVERTISED services and nothing else.
- Legacy unicast (RFC 6762 §6.7): the query is sent from an EPHEMERAL
  source port, so compliant responders (avahi/mDNSResponder) reply in
  one unicast packet to that port — no privileged :5353 bind needed.
  Multicast membership is additionally joined so responders that
  answer by multicast are also seen; a failed join degrades to
  unicast-only, honestly noted.
- Every record is an ADVERTISEMENT claimed BY the device itself:
  ``verified`` stays false and NO trust or authorization follows.
- Malformed packets are skipped and COUNTED honestly; socket errors
  and timeouts surface as precise failures, never as empty successes
  pretending a quiet network is an empty one.

``discover()`` is SAFE/read-only; Core policy still gates who may call
it and with what parameters.
"""
from __future__ import annotations

import re
import socket
import struct
import threading
import time
from typing import Callable, Optional

#: mDNS multicast group/port (fixed by RFC 6762)
_MDNS_ADDR, _MDNS_PORT = "224.0.0.251", 5353

#: bounded default allowlist — services a local assistant actually
#: needs (media receivers, printers, file shares). Additional types
#: only enter through the `_services._dns-sd._udp.local` enumeration.
DEFAULT_SERVICES = (
    "_googlecast._tcp", "_airplay._tcp", "_raop._tcp", "_ipp._tcp",
    "_printer._tcp", "_sftp-ssh._tcp", "_smb._tcp",
    "_spotify-connect._tcp", "_DIAL-service._tcp", "_hap._tcp",
)
ENUMERATION_SERVICE = "_services._dns-sd._udp.local"
MAX_RESULTS = 64          # hard cap on returned advertisements
MAX_TYPES = 12            # hard cap on queried service types

#: standard mDNS service-type names only (_name._tcp / _name._udp);
#: anything else is refused rather than scanned
TYPE_RE = re.compile(r"^_[A-Za-z0-9-]+(\._(tcp|udp))?$", re.I)

_SOCK: Optional[Callable] = None    # test hook: socket-socket factory


# ------------------------------------------------------------ DNS wire

def _enc_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not raw:
            continue                    # leading empty label (mDNS enum)
        if len(raw) > 63:
            raise ValueError(f"bad label {label!r}")
        out += bytes([len(raw)]) + raw
    if not out:
        raise ValueError("empty name")
    return out + b"\x00"


def _build_query(types: list[str]) -> bytes:
    """One query, one question per type. Class 0x8001 = IN with the QU
    bit — legacy unicast response requested (RFC 6762 §6.7)."""
    questions = b""
    for t in types:
        questions += _enc_name(
            t if t.endswith(".local") else t + ".local") \
            + struct.pack(">HH", 12, 0x8001)     # PTR, IN|QU
    return struct.pack(">HHHHHH", 0x2b2b, 0, len(types), 0, 0, 0) \
        + questions


def _dec_name(buf: bytes, off: int):
    """Decode a (possibly compressed) DNS name stored anywhere in the
    packet. Returns (name_without_trailing_dot, next_offset) or
    ("", -1) on malformed input."""
    labels: list[str] = []
    jumps = 0
    nxt: Optional[int] = None
    while True:
        if off < 0 or off >= len(buf):
            return "", -1
        ln = buf[off]
        if ln & 0xC0 == 0xC0:
            if off + 1 >= len(buf) or jumps > 12:
                return "", -1
            ptr = ((ln & 0x3F) << 8) | buf[off + 1]
            nxt = nxt if nxt is not None else off + 2
            if ptr >= off:
                return "", -1                    # anti-loop
            off, jumps = ptr, jumps + 1
            continue
        if ln == 0:
            rest = nxt if nxt is not None else off + 1
            return ".".join(labels), rest
        if ln > 63 or off + 1 + ln > len(buf):
            return "", -1
        labels.append(buf[off + 1:off + 1 + ln].decode("utf-8",
                                                       "replace"))
        off += 1 + ln


def _parse_response(buf: bytes):
    """Parse ONE DNS response packet.

    Returns (advertisements, enumerated_service_types). Raises
    ValueError on malformed packets — the caller counts them.

    Advertisement shape: {service_type, instance, host?, port?, addr?}
    — every field came from the device's own response.
    """
    if len(buf) < 12:
        raise ValueError("short packet")
    flags, qd, an = struct.unpack(">HHH", buf[2:8])
    if not flags & 0x8000:
        raise ValueError("not a response")
    off = 12
    for _ in range(qd):
        _, off = _dec_name(buf, off)
        if off < 0 or off + 4 > len(buf):
            raise ValueError("bad question")
        off += 4
    rrs: list = []
    for _ in range(an):
        name, off = _dec_name(buf, off)
        if off < 0 or off + 10 > len(buf):
            raise ValueError("bad rr header")
        rtype, _rclass, _ttl, rdlen = struct.unpack(
            ">HHIH", buf[off:off + 10])
        rd = buf[off + 10:off + 10 + rdlen]
        if len(rd) != rdlen:
            raise ValueError("rr overrun")
        rrs.append((name, rtype, off + 10, rd))
        off = off + 10 + rdlen
    ads: list[dict] = []
    types_seen: list[str] = []
    for name, rtype, rdata_off, rd in rrs:
        if rtype != 12 or not rd:
            continue                              # only PTR advertises
        tgt, _ = _dec_name(buf, rdata_off)
        if not tgt:
            raise ValueError("bad ptr rdata")
        tgt_r = re.sub(r"\.local\.?$", "", tgt.rstrip("."))
        if name.rstrip(".") == ENUMERATION_SERVICE:
            m = re.match(r"^(?:\.?)((?:_[A-Za-z0-9-]+)(?:\._"
                         r"[A-Za-z0-9-]+)*)$", tgt_r, re.I)
            if m:
                typ = m.group(1).lower()
                types_seen.append(typ if typ.startswith(".")
                                  else "." + typ)
            continue
        m = re.match(r"^(.+?)((?:\._[A-Za-z0-9-]+)+)$", tgt_r, re.I)
        if not m:
            continue
        ad = {"service_type": m.group(2).lower(),
              "instance": m.group(1)}
        # enrich from records grouped with this instance (standard
        # DNS-SD grouping): SRV/port+host and A/addr
        for n2, t2, o2, rd2 in rrs:
            if t2 == 33 and rd2 and len(rd2) >= 6 \
                    and "port" not in ad \
                    and ad["instance"] in n2:
                host, _ = _dec_name(buf, o2 + 6)   # pri+weight+port = 6
                if host:
                    ad["host"] = host.rstrip(".")
                ad["port"] = struct.unpack(">H", rd2[4:6])[0]
            elif t2 == 1 and rd2 and len(rd2) == 4 \
                    and ad.get("host") and ad["host"] in n2:
                ad["addr"] = ".".join(str(b) for b in rd2)
        ads.append(ad)
    return ads, types_seen


# ------------------------------------------------------------ discovery

def discover(services: Optional[list] = None,
             timeout_: float = 2.0,
             stop: Optional[threading.Event] = None,
             max_results: int = MAX_RESULTS) -> dict:
    """One bounded DNS-SD discovery pass. Always returns a dict with
    ``available`` and, on success, ALREADY-DE-DUPLICATED
    advertisements — each simply what a device SAID about itself.

    ``services`` is validated against TYPE_RE and capped at MAX_TYPES;
    anything else is a refusal, never a broader scan.
    """
    stop = stop or threading.Event()
    req = list(services) if services else list(DEFAULT_SERVICES)
    clean: list[str] = []
    for t in req:
        if not TYPE_RE.match(t):
            return {"available": False,
                    "reason": f"service type refused: {t!r} — only "
                    "standard _service._tcp/_udp names are scanned, "
                    "no arbitrary ports or subnets"}
        if len(clean) < MAX_TYPES:
            clean.append(t)
        if stop.is_set():
            break
    if stop.is_set():
        return {"available": False, "reason": "cancelled before start"}
    all_types = clean + [ENUMERATION_SERVICE]
    query = _build_query(all_types)

    packets = malformed = 0
    notes: list = []
    sock = None
    try:
        sock = _SOCK() if _SOCK is not None \
            else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))                        # ephemeral port only
        try:
            sock.setsockopt(socket.IPPROTO_IP,
                            socket.IP_MULTICAST_LOOP, 0)
            import sys
            if sys.platform != "linux":
                raise OSError("membership join only wired for linux")
            mreq = socket.inet_aton(_MDNS_ADDR) \
                + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP,
                            socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            note = ("multicast join unavailable ("
                    + e.__class__.__name__
                    + "); unicast legacy replies still expected "
                    "per RFC 6762 §6.7")
            notes.append(note)
        sock.settimeout(0.25)
        sock.sendto(query, (_MDNS_ADDR, _MDNS_PORT))
        deadline = time.monotonic() + timeout_
        ads: list = []
        seen: set = set()
        types_seen: list = []
        while time.monotonic() < deadline and not stop.is_set() and \
                len(ads) < max_results:
            try:
                data, _peer = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError as e:
                notes.append(f"recv stopped: {e.__class__.__name__}")
                break
            packets += 1
            try:
                pa, pt = _parse_response(data)
            except ValueError as e:
                malformed += 1
                notes.append(f"malformed packet skipped: {e}")
                continue
            for a in pa:
                key = (a["service_type"], a["instance"])
                if key not in seen:
                    seen.add(key)
                    ads.append({**a, "verified": False,
                                "identity_status": "advertised-"
                                "unverified (claimed by the device "
                                "itself; no handshake performed)"})
            for t in pt:
                if t not in types_seen:
                    types_seen.append(t)
        return {
            "available": True,
            "scanner": "mdns-probe",
            "queried_types": clean,
            "enumeration_query": ENUMERATION_SERVICE,
            "advertisements": ads,
            "service_types_seen": types_seen,
            "packets_received": packets,
            "malformed_packets": malformed,
            "notes": notes + ["zero packets can mean: no responders "
                              "on this network, mDNS filtered, or a "
                              "quiet LAN — this scan CANNOT "
                              "distinguish those"],
            "boundaries": ["mDNS .local domain only; no port "
                           "scan/subnet sweep", "advertisements are "
                           "claims — never trust or authorization"],
        }
    except OSError as e:
        return {"available": False,
                "reason": f"mDNS socket unavailable: "
                f"{e.__class__.__name__}: {e}",
                "packets_received": packets,
                "malformed_packets": malformed,
                "notes": notes}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
