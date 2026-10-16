"""Tanglish normalization + language-aware intent parsing.

Normalization is deterministic and dependency-free (core/tanglish.py);
the parser matches on the normalized form while proposals keep the
ORIGINAL utterance. Parity rule: Tanglish must resolve exactly like
the equivalent English — including clarification fallbacks — and
never gain authority English lacks (approvals still mandatory).
"""
import pytest

from core.intent import LocalIntentParser, ParserContext, ParserDevice
from core.tanglish import normalize_utterance


def _ctx(caps=None):
    caps = caps or {
        "system.battery", "system.wifi.set", "system.wifi.state",
        "system.app.open", "device.list", "assistant.help",
    }
    return ParserContext(
        devices=[ParserDevice(device_id="phone-1", kind="android"),
                 ParserDevice(device_id="laptop-1", kind="linux")],
        default_device="",
        known_capabilities=caps, capability_schemas={})


# ---------- normalization unit behavior ----------

def test_examples_from_spec():
    cases = {
        "phone battery evlo irukku": ("phone battery how much is",
                                      "tanglish"),
        "wifi on pannu": ("turn on wifi", "tanglish"),
        "laptop la file open pannu": ("laptop file open", "tanglish"),
        "indha task cancel pannu": ("this task cancel", "tanglish"),
        "enakku weather sollu": ("for me weather", "tanglish"),
        "enna panra": ("what", "tanglish"),
    }
    for src, (want_norm, want_lang) in cases.items():
        n = normalize_utterance(src)
        assert n.normalized == want_norm, (src, n.normalized)
        assert n.language == want_lang, (src, n.language)
        assert n.original == src  # original always preserved
        assert n.mapped is True


def test_english_passthrough_byte_identical():
    for src in ["what is the battery level", "turn on wifi",
                "open https://example.com/x", "list files in /tmp"]:
        n = normalize_utterance(src)
        assert n.normalized == src
        assert n.language == "en" and n.mapped is False


def test_protected_spans_never_rewritten():
    n = normalize_utterance("open report.pdf pannu")
    assert "report.pdf" in n.normalized
    n = normalize_utterance('open "my notes.txt" pannu')
    assert "my notes.txt" in n.normalized
    n = normalize_utterance("open https://example.com/a pannu")
    assert "https://example.com/a" in n.normalized
    n = normalize_utterance("check myFile2 status pannu")
    assert "myFile2" in n.normalized


def test_tamil_script_detected_and_minimally_mapped():
    n = normalize_utterance("பேட்டரி நிலை என்ன")
    assert n.language == "ta"
    assert "battery" in n.normalized and "what" in n.normalized
    # Unmapped Tamil stays honest: flagged, parser clarifies downstream.
    m = normalize_utterance("வணக்கம் நண்பா")
    assert m.language == "ta" and m.mapped is False


def test_particles_gated_to_tamil_context():
    # Standalone "la" survives in pure English (formula, gala, ...).
    assert normalize_utterance("check the formula").mapped is False
    assert "formula" in normalize_utterance("check the formula").normalized
    # Bare "laptop la" with no other marker passes through unmapped
    # (conservative: no false location parsing from one particle).
    assert normalize_utterance("laptop la").mapped is False
    # ... but drops as a location marker once Tamil mapped.
    assert normalize_utterance("laptop la pannu").normalized == "laptop"


def test_dangerous_tanglish_never_laundered():
    # A destructive request in Tanglish must not normalize into
    # something executable-looking; parser must refuse or clarify.
    p = LocalIntentParser().parse("phone format pannu", _ctx())
    assert p.capability == ""  # no alias for destructive ops


def test_non_string_never_raises():
    for bad in (None, 123, b"\xff\xfe"):
        n = normalize_utterance(bad)
        assert isinstance(n.normalized, str)


# ---------- parser parity: same meaning, same verdict ----------

def _parse(text):
    return LocalIntentParser().parse(text, _ctx())


def test_battery_question_parity():
    en = _parse("phone battery level")
    tg = _parse("phone battery evlo irukku")
    assert en.capability == tg.capability == "system.battery"
    assert tg.utterance == "phone battery evlo irukku"  # original kept
    assert tg.confidence < en.confidence  # translated uncertainty


def test_wifi_switch_parity_with_direction():
    en = _parse("turn on wifi")
    tg = _parse("wifi on pannu")
    assert tg.capability == en.capability == "system.wifi.set"
    assert tg.parameters.get("enabled") is True
    off = _parse("wifi off pannu")
    assert off.capability == "system.wifi.set"
    assert off.parameters.get("enabled") is False


def test_ambiguous_stays_ambiguous_in_both_languages():
    # "laptop file open" names no app — clarify, like English does.
    for text in ("laptop la file open pannu", "open file on laptop"):
        p = _parse(text)
        assert p.needs_clarification is True, text
        assert p.capability in ("", "system.app.open"), text


def test_unsupported_stays_unsupported():
    for text in ("enakku weather sollu", "tell me the weather"):
        p = _parse(text)
        assert p.capability == "" and p.needs_clarification, text


def test_tamil_script_question_parses():
    p = _parse("பேட்டரி நிலை என்ன")
    assert p.capability == "system.battery", p
    assert "பேட்டரி" in p.utterance  # original script preserved


def test_approval_still_mandatory_after_normalization():
    # A Tanglish-gated radio change must face the same approval gate
    # as its English twin: parse grants nothing by itself.
    from core.policy import PolicyEngine, RiskLevel
    pol = PolicyEngine()
    for text in ("turn on wifi", "wifi on pannu"):
        p = _parse(text)
        assert p.capability == "system.wifi.set"
        d = pol.decide("user", capability=f"tool:{p.capability}",
                       device_id="laptop-1", resource=p.capability,
                       risk="confirm")
        assert d.allow is False and d.requires_approval is True, text


def test_normalization_never_authorizes():
    # The Normalization object carries no authority fields at all.
    n = normalize_utterance("wifi off pannu")
    assert not hasattr(n, "allow") and not hasattr(n, "approve")
    assert not hasattr(n, "capability") and not hasattr(n, "grant")


# ---------- turn-level integration (real stack, no mocks) ----------

def _live_stack():
    from core.app import build_stack
    from core.models import DeviceKind, DeviceState
    stack = build_stack(fabric_db="")
    for dev, kind in (("laptop-1", "linux"), ("phone-1", "android")):
        code = stack["device_auth"].enroll(dev, DeviceKind(kind))
        stack["device_auth"].claim(code)
        stack["devices"].register(DeviceState(
            device_id=dev, kind=DeviceKind(kind),
            capabilities=["system.battery", "system.wifi.set"],
            online=True, status="online"))
    stack["fabric"].advertise(
        [{"id": "system.battery", "name": "system.battery",
          "descriptor_version": "1", "version": "1.0.0",
          "risk": "safe"}], "phone-1")
    for cap, risk in (("system.battery", "safe"),
                      ("system.wifi.set", "confirm")):
        stack["fabric"].advertise(
            [{"id": cap, "name": cap, "descriptor_version": "1",
              "version": "1.0.0", "risk": risk}], "laptop-1")
    return stack


def test_turn_tanglish_battery_matches_english():
    # Resolution-level parity (no live agent, so the full turn would
    # block on execution): equivalent meaning resolves equivalently.
    from core.intent import LocalIntentParser, build_context
    from core.resolver import CapabilityResolver
    stack = _live_stack()
    resolver = CapabilityResolver(stack["fabric"])
    results = {}
    for u in ("phone battery level", "phone battery evlo irukku"):
        ctx = build_context(stack["fabric"])
        p = LocalIntentParser().parse(u, ctx)
        assert p.capability == "system.battery", (u, p)
        assert p.utterance == u  # original preserved, not normalized
        r = resolver.resolve_proposal(
            {"preferred_capability": p.capability,
             "preferred_device": p.preferred_device or "",
             "constraints": {}})
        results[u] = r
    en, tg = (results["phone battery level"],
              results["phone battery evlo irukku"])
    assert en.status == tg.status
    assert en.device_id == tg.device_id


def test_turn_tanglish_wifi_holds_for_approval():
    from core.intent import IntentTurnService, LocalIntentParser
    from core.resolver import CapabilityResolver
    stack = _live_stack()
    svc = IntentTurnService(stack["fabric"],
                            CapabilityResolver(stack["fabric"]),
                            LocalIntentParser(), stack["audit"])
    out = svc.handle_utterance("wifi on pannu")
    assert out["executed"] is False  # never runs from a chat request
    prop = out["proposal"]
    assert prop["capability"] == "system.wifi.set"
    # Approval path engaged (hold record or explicit approval message),
    # identical in kind to the English twin.
    en = svc.handle_utterance("turn on wifi")
    assert (out["resolution"] or {}).get("status") == \
        (en["resolution"] or {}).get("status")


def test_reorder_never_destroys_english_alias_order():
    # "flight mode on" matches an alias verbatim; normalization's
    # device-switch reorder must not steal that match.
    from core.intent import LocalIntentParser
    caps = {"system.airplane_mode.set"}
    ctx = ParserContext(devices=[], default_device="",
                        known_capabilities=caps, capability_schemas={})
    for text in ("flight mode on", "flight mode off"):
        p = LocalIntentParser().parse(text, ctx)
        assert p.capability == "system.airplane_mode.set", text
    # And the Tanglish SOV order still normalizes into a match.
    p = LocalIntentParser().parse(
        "airplane mode on pannu",
        ParserContext(devices=[], default_device="",
                      known_capabilities=caps, capability_schemas={}))
    assert p.capability == "system.airplane_mode.set"
