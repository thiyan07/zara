"""Standalone tests for core/uicontrol.py. No mocks of other Zara modules."""
import pytest

from core.uicontrol import (
    AMBIGUOUS,
    CRASH,
    DIALOG,
    MATCH,
    NOT_FOUND,
    PACKAGE_JUMP,
    PROGRESS,
    REPEAT_ACTION,
    REPEAT_SNAPSHOT,
    STOP,
    REDACTED,
    ElementTarget,
    MatchResult,
    PlannedAction,
    StuckDetector,
    UiElement,
    UiSnapshot,
    check_expected_package,
    class_of,
    evaluate_probe,
    is_stale,
    match,
    needs_reinspect,
    normalize_text,
    parse_snapshot,
)


def el(i, **kw):
    base = {"id": f"e{i}", "role": "Button", "text": f"Item {i}",
            "bounds": [0, i * 10, 100, i * 10 + 8]}
    base.update(kw)
    return base


def snap(elements, package_name="com.example.app", **kw):
    d = {"device_id": "dev1", "package_name": package_name,
         "screen_w": 1080, "screen_h": 2400, "elements": elements}
    d.update(kw)
    return parse_snapshot(d)


# --- 1. bounds / scrub / stable id -----------------------------------------

def test_51st_element_dropped():
    s = snap([el(i) for i in range(60)])
    assert len(s.elements) == 50
    assert s.elements[-1].id == "e49"


def test_121_char_text_truncated():
    s = snap([el(0, text="x" * 121)])
    assert len(s.elements[0].text) == 120


def test_other_field_limits_truncated():
    s = snap([el(0, role="r" * 41, resource_id="a" * 129,
                  content_desc="c" * 121)])
    e = s.elements[0]
    assert len(e.role) == 40
    assert len(e.resource_id) == 128
    assert len(e.content_desc) == 120


def test_bad_nodes_skipped_never_raise():
    s = snap([
        el(0),
        {"no": "id"},                       # missing id
        {"id": "bad1", "bounds": [1, 2]},   # short bounds
        {"id": "bad2", "bounds": ["a"] * 4},  # non-int bounds
        {"id": "", "text": "empty id"},      # empty id
        {"id": "bad3", "text": 123},         # wrong type
        "just a string",                     # non-dict node
        None,
        el(1),
    ])
    assert [e.id for e in s.elements] == ["e0", "e1"]


def test_secret_values_scrubbed():
    s = snap([el(0, text="password=hunter2"),
              el(1, content_desc="API_KEY abc123"),
              el(2, resource_id="com.app:id/user_token"),
              el(3, text="hello world")])
    assert s.elements[0].text == "[redacted]"
    assert s.elements[1].content_desc == "[redacted]"
    assert s.elements[2].resource_id == "[redacted]"
    assert s.elements[3].text == "hello world"


def test_snapshot_id_stable_across_key_order():
    a = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 2,
         "elements": [{"id": "e", "text": "t", "bounds": [0, 0, 1, 1]}]}
    b = {"elements": [{"bounds": [0, 0, 1, 1], "text": "t", "id": "e"}],
         "screen_h": 2, "screen_w": 1, "package_name": "p", "device_id": "d"}
    assert parse_snapshot(a).snapshot_id == parse_snapshot(b).snapshot_id
    assert parse_snapshot(a).recompute_id() == parse_snapshot(b).recompute_id()


def test_snapshot_id_changes_with_content():
    s1 = snap([el(0)])
    s2 = snap([el(0, text="different")])
    assert s1.snapshot_id != s2.snapshot_id


def test_volatile_fields_excluded_from_id():
    s1 = snap([el(0)], timestamp=1.0, screenshot_available=False)
    s2 = snap([el(0)], timestamp=999.0, screenshot_available=True)
    assert s1.snapshot_id == s2.snapshot_id


def test_class_alias_accepted_for_role():
    s = snap([{"id": "c1", "class": "TextView", "text": "hi"}])
    assert s.elements[0].role == "TextView"


def test_extra_forbidden():
    with pytest.raises(Exception):
        UiElement(id="x", bogus_field=1)
    with pytest.raises(Exception):
        parse_snapshot({"device_id": "d", "bogus": 1})


# --- 2. matcher --------------------------------------------------------------

def _multi():
    return snap([
        el(0, resource_id="com.app:id/ok", text="Submit"),
        el(1, resource_id="com.app:id/cancel", text="Submit",
           content_desc="Cancel button"),
        el(2, text="  HELLO   World  ",
           content_desc="greeting"),
    ])


def test_matcher_resource_id_beats_text():
    r = match(_multi(), ElementTarget(resource_id="com.app:id/cancel",
                                     text="nope"))
    assert r.status == MATCH and r.element.id == "e1"


def test_matcher_content_desc_beats_text():
    r = match(_multi(), ElementTarget(content_desc="Cancel button",
                                      text="Submit"))
    # resource_id level: no target resource_id -> skipped; content_desc hits e1
    assert r.status == MATCH and r.element.id == "e1"


def test_matcher_exact_text():
    r = match(_multi(), ElementTarget(text="Submit", role="Button"))
    # exact text matches e0 AND e1 -> ambiguous (priority level has 2)
    assert r.status == AMBIGUOUS
    assert len(r.candidates) == 2


def test_matcher_exact_text_single():
    r = match(_multi(), ElementTarget(text="nope", normalized_text="hello world"))
    assert r.status == MATCH and r.element.id == "e2"


def test_matcher_normalized_text():
    assert normalize_text("  HeLLo\t World\n") == "hello world"
    s = snap([el(0, text="  HELLO   World  ")])
    r = match(s, ElementTarget(normalized_text="hello world"))
    assert r.status == MATCH and r.element.id == "e0"


def test_matcher_role_with_index_hint():
    s = snap([el(i, role="Button") for i in range(3)])
    r = match(s, ElementTarget(role="button", index_hint=2))
    assert r.status == MATCH and r.element.id == "e2"


def test_matcher_ambiguity_capped_no_indexes():
    s = snap([el(i, role="Button") for i in range(6)])
    r = match(s, ElementTarget(role="Button"))
    assert r.status == AMBIGUOUS
    assert len(r.candidates) == 5
    assert isinstance(r, MatchResult)


def test_matcher_not_found_and_empty_target():
    s = snap([el(0)])
    assert match(s, ElementTarget(text="zzz")).status == NOT_FOUND
    assert match(s, ElementTarget()).status == NOT_FOUND


def test_matcher_priority_fallthrough():
    # resource_id misses, content_desc hits
    s = snap([el(0, resource_id="a", content_desc="found me")])
    r = match(s, ElementTarget(resource_id="missing", content_desc="found me"))
    assert r.status == MATCH and r.element.id == "e0"


def test_matcher_coordinates_never_blind():
    s = snap([el(0, bounds=[10, 10, 100, 100])])
    r = match(s, ElementTarget(x=50, y=50))  # coords only
    assert r.status == NOT_FOUND


def test_matcher_coordinates_confirm_near():
    s = snap([el(0, resource_id="r", bounds=[10, 10, 100, 100])])
    r = match(s, ElementTarget(resource_id="r", x=50, y=50))
    assert r.status == MATCH
    r2 = match(s, ElementTarget(resource_id="r", x=500, y=500))
    assert r2.status == NOT_FOUND  # matched element not within 8dp


def test_matcher_tolerance_boundary():
    s = snap([el(0, resource_id="r", bounds=[10, 10, 100, 100])])
    ok = match(s, ElementTarget(resource_id="r", x=108, y=50))  # 8dp away
    far = match(s, ElementTarget(resource_id="r", x=109, y=50))  # 9dp away
    assert ok.status == MATCH
    assert far.status == NOT_FOUND


def test_matcher_accepts_dict_target_and_skips_garbage():
    s = snap([el(0, text="hi")])
    assert match(s, {"text": "hi"}).status == MATCH
    assert match(s, {"nonsense": 1}).status == NOT_FOUND  # invalid target shape


# --- 3. privilege classes -----------------------------------------------------

def test_privilege_read():
    assert class_of("gui.screen.inspect") == "READ"
    assert class_of("gui.state.verify") == "READ"


def test_privilege_interact():
    for c in ("gui.tap", "gui.scroll", "gui.text_input", "gui.app.launch",
              "gui.navigation.back", "gui.navigation.open"):
        assert class_of(c) == "INTERACT", c


def test_privilege_sensitive():
    for c in ("files.send", "media.upload", "docs.download", "link.share",
              "form.submit"):
        assert class_of(c) == "SENSITIVE", c


def test_privilege_destructive():
    for c in ("files.delete", "app.uninstall", "store.purchase",
              "device.security_scan", "user.account_reset", "myaccount.info"):
        assert class_of(c) == "DESTRUCTIVE", c


def test_privilege_destructive_wins_and_unknown():
    assert class_of("vault.account.send") == "DESTRUCTIVE"  # not SENSITIVE
    assert class_of("something.else") == "UNKNOWN"
    assert class_of("") == "UNKNOWN"


# --- 4. stuck detector ----------------------------------------------------------

def _obs(det, sid, action="tap", pkg="com.example.app"):
    return det.observe({"snapshot_id": sid, "package_name": pkg}, action)


def test_stuck_progress():
    d = StuckDetector()
    assert _obs(d, "s1", "tap") == PROGRESS
    assert _obs(d, "s2", "scroll") == PROGRESS


def test_stuck_repeat_snapshot():
    d = StuckDetector()
    assert _obs(d, "s1", "tap") == PROGRESS
    assert _obs(d, "s1", "scroll") == PROGRESS
    assert _obs(d, "s1", "input") == REPEAT_SNAPSHOT


def test_stuck_repeat_action():
    d = StuckDetector()
    assert _obs(d, "s1", "tap") == PROGRESS
    assert _obs(d, "s1", "tap") == PROGRESS
    assert _obs(d, "s1", "tap") == REPEAT_ACTION


def test_stuck_same_action_changing_screen_is_progress():
    d = StuckDetector()
    assert _obs(d, "s1", "tap") == PROGRESS
    assert _obs(d, "s2", "tap") == PROGRESS
    assert _obs(d, "s3", "tap") == PROGRESS


def test_stuck_package_jump():
    d = StuckDetector()
    assert _obs(d, "s1", "tap", pkg="com.a") == PROGRESS
    assert _obs(d, "s2", "tap", pkg="com.b") == PACKAGE_JUMP


def test_stuck_dialog_packages():
    for pkg in ("com.android.permissioncontroller", "com.android.systemui"):
        d = StuckDetector()
        assert d.observe({"snapshot_id": "s", "package_name": pkg},
                         "tap") == DIALOG


def test_stuck_crash_target_gone():
    d = StuckDetector(target_package="com.game")
    assert d.observe({"snapshot_id": "s1",
                      "package_name": "com.game"}, "tap") == PROGRESS
    assert d.observe({"snapshot_id": "s2", "package_name": ""}, "tap") == CRASH


def test_stuck_unexpected_package_with_target():
    d = StuckDetector(target_package="com.game")
    assert d.observe({"snapshot_id": "s1",
                      "package_name": "com.other"}, "tap") == PACKAGE_JUMP


def test_stuck_8_step_cap():
    d = StuckDetector()
    states = [_obs(d, f"s{i}", f"a{i}") for i in range(8)]
    assert all(s == PROGRESS for s in states)
    assert _obs(d, "s8", "a8") == STOP
    assert _obs(d, "s9", "a9") == STOP


# --- 5. probe evaluator ----------------------------------------------------------

def good_probe():
    return {"service_enabled": True, "can_retrieve": True, "can_act": True,
            "api_level": 33, "ping_ms": 150}


def test_probe_supported():
    ok, reason = evaluate_probe(good_probe())
    assert ok is True and reason == "supported"


def test_probe_service_disabled():
    p = good_probe()
    p["service_enabled"] = False
    ok, _ = evaluate_probe(p)
    assert ok is False


def test_probe_cannot_retrieve():
    p = good_probe()
    p["can_retrieve"] = False
    assert evaluate_probe(p)[0] is False


def test_probe_cannot_act():
    p = good_probe()
    p["can_act"] = False
    assert evaluate_probe(p)[0] is False


def test_probe_api_level_missing():
    p = good_probe()
    del p["api_level"]
    ok, reason = evaluate_probe(p)
    assert ok is False and "api_level" in reason


def test_probe_ping_branches():
    p = good_probe()
    del p["ping_ms"]
    assert evaluate_probe(p)[0] is False
    p["ping_ms"] = 2000
    assert evaluate_probe(p)[0] is False  # boundary: must be < 2000
    p["ping_ms"] = 5000
    ok, reason = evaluate_probe(p)
    assert ok is False and "ping" in reason
    p["ping_ms"] = "fast"
    assert evaluate_probe(p)[0] is False
    assert evaluate_probe("nope")[0] is False


def test_probe_reasons_deterministic():
    assert evaluate_probe(good_probe()) == evaluate_probe(good_probe())
    assert evaluate_probe({}) == evaluate_probe({})


# --- 6. action structures ----------------------------------------------------------

def test_planned_action_valid():
    a = PlannedAction(capability="gui.tap", target={"text": "OK"},
                      timeout_s=30, retry=1, snapshot_before_id="abc")
    assert a.timeout_s == 30 and a.retry == 1


def test_planned_action_bounds_rejected():
    with pytest.raises(Exception):
        PlannedAction(capability="gui.tap", timeout_s=61)
    with pytest.raises(Exception):
        PlannedAction(capability="gui.tap", retry=3)
    with pytest.raises(Exception):
        PlannedAction(capability="gui.tap", bogus=1)


def test_needs_reinspect_stale_and_status():
    assert needs_reinspect({"ok": True, "snapshot_before_id": "a",
                            "snapshot_after_id": "b"}) is True  # stale
    assert needs_reinspect({"ok": True, "snapshot_before_id": "a",
                            "snapshot_after_id": "a"}) is False
    assert needs_reinspect({"status": "AMBIGUOUS"}) is True
    assert needs_reinspect({"status": "stale"}) is True
    assert needs_reinspect({"ok": True, "reinspect": True}) is True
    assert needs_reinspect({"ok": True}) is False
    assert needs_reinspect("garbage") is True  # fail closed


# --- 7. Rung-3: truncation metadata ------------------------------------------

def test_truncation_elements_capped_at_50():
    s = snap([el(i) for i in range(100)])
    assert len(s.elements) == 50
    assert s.truncation["elements_truncated"] == 50
    assert s.truncation["depth_exceeded"] is False


def test_truncation_strings_tracked():
    s = snap([el(0, text="x" * 130, role="r" * 50)])
    assert s.truncation["strings_truncated"] >= 2  # text and role both truncated


def test_truncation_empty_tree():
    s = snap([])
    assert len(s.elements) == 0
    assert s.truncation == {"elements_truncated": 0, "strings_truncated": 0, "depth_exceeded": False}


# --- 8. Rung-3: sensitive field detection/redaction --------------------------

def test_sensitive_field_set_and_redacted_in_match():
    # Use a value that doesn't trigger secret scrubber, but tests sensitive flag
    s = snap([el(0, text="my_custom_field", sensitive=True)])
    # normalized() includes sensitive flag
    assert s.elements[0].sensitive is True
    # match with normalized_text should see redacted for sensitive elements
    r = match(s, ElementTarget(normalized_text="my custom field"))
    # Exact text match works with actual stored text
    r_exact = match(s, ElementTarget(text="my_custom_field"))
    assert r_exact.status == MATCH
    # Normalized match sees "[redacted]" for sensitive elements
    r_norm = match(s, ElementTarget(normalized_text="[redacted]"))
    assert r_norm.status == MATCH
    r_norm_fail = match(s, ElementTarget(normalized_text="my custom field"))
    assert r_norm_fail.status == NOT_FOUND


def test_sensitive_false_by_default():
    s = snap([el(0, text="hello")])
    assert s.elements[0].sensitive is False


def test_visible_long_clickable_editable_defaults():
    s = snap([el(0)])
    assert s.elements[0].visible is True
    assert s.elements[0].long_clickable is False
    assert s.elements[0].editable is False


def test_sensitive_field_can_be_set_explicitly():
    s = snap([el(0, visible=False, long_clickable=True, editable=True)])
    assert s.elements[0].visible is False
    assert s.elements[0].long_clickable is True
    assert s.elements[0].editable is True


# --- 9. Rung-3: expected_package guard ---------------------------------------

def test_expected_package_match():
    s = snap([el(0)], package_name="com.example.app")
    assert check_expected_package(s, "com\\.example\\..*") == "MATCH"
    assert check_expected_package(s, "") == "MATCH"
    assert check_expected_package(s, None) == "MATCH"


def test_expected_package_mismatch():
    s = snap([el(0)], package_name="com.evil.app")
    assert check_expected_package(s, "com\\.example\\..*") == "UNEXPECTED_PACKAGE"
    assert check_expected_package(s, "com\\.other\\..*") == "UNEXPECTED_PACKAGE"


def test_expected_package_malformed_regex():
    s = snap([el(0)], package_name="com.example.app")
    # Invalid regex should fail closed
    assert check_expected_package(s, "[invalid") == "UNEXPECTED_PACKAGE"


def test_match_returns_unexpected_package():
    s = snap([el(0, text="OK")], package_name="com.evil.app")
    r = match(s, ElementTarget(text="OK"), expected_package="com\\.example\\..*")
    assert r.status == "UNEXPECTED_PACKAGE"


def test_match_returns_match_when_package_matches():
    s = snap([el(0, text="OK")], package_name="com.example.app")
    r = match(s, ElementTarget(text="OK"), expected_package="com\\.example\\..*")
    assert r.status == MATCH


# --- 10. Rung-3: stale snapshot rejection ------------------------------------

def test_is_stale_true_when_mismatch():
    assert is_stale("abc123", "def456") is True


def test_is_stale_false_when_match():
    assert is_stale("abc123", "abc123") is False


def test_is_stale_false_when_empty():
    assert is_stale("", "abc123") is False
    assert is_stale("abc123", "") is False
    assert is_stale("", "") is False


def test_match_returns_stale_snapshot():
    s = snap([el(0, text="OK")])
    r = match(s, ElementTarget(text="OK"), expected_snapshot_id="different_id")
    assert r.status == "STALE_SNAPSHOT"


def test_match_passes_when_snapshot_id_matches():
    s = snap([el(0, text="OK")])
    r = match(s, ElementTarget(text="OK"), expected_snapshot_id=s.snapshot_id)
    assert r.status == MATCH


# --- 11. Rung-3: Unicode normalization (NFC) ---------------------------------

def test_normalize_text_nfc():
    # é can be represented as U+00E9 (NFC) or U+0065 U+0301 (NFD)
    nfc = "café"  # NFC
    nfd = "cafe\u0301"  # NFD
    assert normalize_text(nfc) == normalize_text(nfd) == "café"


def test_normalize_text_strips_null_bytes():
    assert normalize_text("hello\x00world") == "hello world"
    assert normalize_text("\x00start") == "start"
    assert normalize_text("end\x00") == "end"


def test_normalize_text_strips_bom():
    assert normalize_text("\ufeffhello") == "hello"
    assert normalize_text("hello\ufeff") == "hello"


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  hello   \t\n  world  ") == "hello world"


def test_normalize_text_lowercases():
    assert normalize_text("HeLLo WoRLD") == "hello world"


# --- 12. Rung-3: max_elements cap --------------------------------------------

def test_parse_snapshot_max_elements_override():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": [{"id": f"e{i}", "text": f"t{i}", "bounds": [0,0,1,1]} for i in range(100)]}
    s = parse_snapshot(data, max_elements=10)
    assert len(s.elements) == 10
    assert s.truncation["elements_truncated"] == 90


def test_parse_snapshot_max_elements_bounded():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": [{"id": f"e{i}", "text": f"t{i}", "bounds": [0,0,1,1]} for i in range(10)]}
    # max_elements=0 should be clamped to 1
    s = parse_snapshot(data, max_elements=0)
    assert len(s.elements) == 1
    # max_elements=100 should be clamped to 50
    s = parse_snapshot(data, max_elements=100)
    assert len(s.elements) == 10  # only 10 available


# --- 13. Rung-3: malformed nodes skipped -------------------------------------

def test_malformed_nodes_skipped_in_parse():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": [
                {"id": "good1", "text": "ok", "bounds": [0,0,1,1]},
                {"not": "valid"},
                {"id": "", "text": "empty id", "bounds": [0,0,1,1]},
                {"id": "good2", "text": "ok", "bounds": [0,0,1,1]},
            ]}
    s = parse_snapshot(data)
    assert [e.id for e in s.elements] == ["good1", "good2"]


def test_non_list_elements_coerced():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": "not a list"}
    s = parse_snapshot(data)
    assert s.elements == []


# --- 14. Rung-3: null bytes / bidi edge cases --------------------------------

def test_null_bytes_in_element_text():
    s = snap([el(0, text="pass\x00word")])
    # Null byte should be stripped in normalization
    r = match(s, ElementTarget(normalized_text="pass word"))
    assert r.status == MATCH


def test_bidi_override_chars():
    # RTL override U+202E and LTR override U+202D
    s = snap([el(0, text="normal\u202ereversed\u202c")])
    r = match(s, ElementTarget(normalized_text="normal reversed"))
    assert r.status == MATCH


def test_bidi_isolate_chars():
    # Isolates U+2066..U+2069
    s = snap([el(0, text="a\u2066b\u2069c")])
    r = match(s, ElementTarget(normalized_text="a b c"))
    assert r.status == MATCH


# --- 15. Rung-3: empty tree --------------------------------------------------

def test_empty_tree_match_not_found():
    s = snap([])
    r = match(s, ElementTarget(text="anything"))
    assert r.status == NOT_FOUND


def test_empty_tree_snapshot_id_stable():
    s1 = snap([])
    s2 = snap([])
    assert s1.snapshot_id == s2.snapshot_id


# --- 16. Rung-3: deep tree (depth cap) ---------------------------------------

def test_deep_nested_elements_not_explicitly_capped_but_bounded_by_max_elements():
    # Elements are flat list in our model; depth is not explicitly tracked
    # but max_elements bounds total nodes
    elements = []
    for i in range(60):
        elements.append({"id": f"e{i}", "text": f"item{i}", "bounds": [0, i*10, 100, i*10+8]})
    data = {"device_id": "d", "package_name": "p", "screen_w": 100, "screen_h": 1000,
            "elements": elements}
    s = parse_snapshot(data)
    assert len(s.elements) == 50
    assert s.truncation["elements_truncated"] == 10


# --- 17. Rung-3: parse_snapshot field filtering ------------------------------

def test_parse_snapshot_exclude_text():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": [{"id": "e1", "text": "secret", "bounds": [0,0,1,1]}]}
    s = parse_snapshot(data, include_text=False)
    assert s.elements[0].text == ""


def test_parse_snapshot_exclude_content_desc():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": [{"id": "e1", "content_desc": "desc", "bounds": [0,0,1,1]}]}
    s = parse_snapshot(data, include_content_description=False)
    assert s.elements[0].content_desc == ""


def test_parse_snapshot_exclude_both():
    data = {"device_id": "d", "package_name": "p", "screen_w": 1, "screen_h": 1,
            "elements": [{"id": "e1", "text": "t", "content_desc": "d", "bounds": [0,0,1,1]}]}
    s = parse_snapshot(data, include_text=False, include_content_description=False)
    assert s.elements[0].text == ""
    assert s.elements[0].content_desc == ""


# --- 18. Rung-3: activity and keyboard_visible fields ------------------------

def test_activity_field_present():
    s = snap([el(0)], activity="com.example.MainActivity")
    assert s.activity == "com.example.MainActivity"


def test_activity_none_when_missing():
    s = snap([el(0)])
    assert s.activity is None


def test_keyboard_visible_field():
    s = snap([el(0)], keyboard_visible=True)
    assert s.keyboard_visible is True


def test_screenshot_available_field():
    s = snap([el(0)], screenshot_available=True)
    assert s.screenshot_available is True
