"""NL routing for the file-workflow and browser-workflow capabilities.

Proves the natural-language requests reach the intended implemented
capability through the normal parser + grounding path (unit-level, same
as test_intent.py). Execution itself is covered by
tests/test_file_workflows.py (real filesystem) and
tests/test_browser_workflows.py (real browser).
"""
import pytest

from core.intent import LocalIntentParser, ParserContext, ParserDevice

LP = LocalIntentParser()


def ctx_fs():
    return ParserContext(
        known_capabilities={"filesystem.search", "filesystem.edit",
                            "filesystem.move", "filesystem.extract"},
        
        capability_schemas={
            "filesystem.search": {"type": "object", "required": ["pattern"],
                                  "properties": {"pattern": {"type": "string"},
                                                 "path": {"type": "string"},
                                                 "max_results": {
                                                     "type": "integer"}}},
            "filesystem.edit": {"type": "object",
                                "required": ["name", "old", "new"],
                                "properties": {"name": {"type": "string"},
                                               "old": {"type": "string"},
                                               "new": {"type": "string"}}},
            "filesystem.move": {"type": "object", "required": ["src", "dst"],
                                "properties": {"src": {"type": "string"},
                                               "dst": {"type": "string"},
                                               "overwrite": {
                                                   "type": "boolean"}}},
            "filesystem.extract": {"type": "object", "required": ["path"],
                                   "properties": {"path": {
                                       "type": "string"}}},
        },
        devices=[ParserDevice(device_id="laptop-1", kind="linux",
                              labels=["laptop"])])


def ctx_browser():
    return ParserContext(
        known_capabilities={"browser.search", "browser.links",
                            "browser.form", "browser.download"},
        capability_schemas={
            "browser.search": {"type": "object", "required": ["term"],
                               "properties": {"term": {"type": "string"}}},
            "browser.download": {"type": "object",
                                 "required": ["url", "name"],
                                 "properties": {"url": {"type": "string"},
                                                "name": {"type": "string"}}},
        },
        devices=[])


# ---------------- filesystem ----------------

def test_find_files_routes_with_pattern():
    p = LP.parse('find files "report"', ctx_fs())
    assert p.needs_clarification is False
    assert p.capability == "filesystem.search"
    assert p.parameters.get("pattern") == "report"


def test_find_files_with_root_and_clarifies_without_pattern():
    p = LP.parse('search files "*.txt" in "projects"', ctx_fs())
    assert p.parameters["pattern"] == "*.txt"
    assert p.parameters["path"] == "projects"
    p2 = LP.parse("search for files", ctx_fs())
    # The clarify carries the missing-question (existing convention:
    # capability is re-merged by the session layer on the answer).
    assert p2.needs_clarification
    assert "search for" in (p2.clarification_reason or "").lower() or \
        "filename" in (p2.clarification_reason or "").lower()


def test_edit_routes_with_replace_phrasing():
    p = LP.parse('edit file: replace "invoice 42" with "invoice 43" '
                 'in "note.txt"', ctx_fs())
    assert p.capability == "filesystem.edit"
    assert p.parameters == {"name": "note.txt", "old": "invoice 42",
                            "new": "invoice 43"}


def test_edit_without_values_clarifies_with_example():
    p = LP.parse("edit file", ctx_fs())
    assert p.needs_clarification
    assert "replace" in (p.clarification_reason or "").lower()


def test_rename_routes_and_clarifies_without_args():
    p = LP.parse('rename file "old.txt" to "new.txt"', ctx_fs())
    assert p.capability == "filesystem.move"
    assert p.parameters == {"src": "old.txt", "dst": "new.txt"}
    p2 = LP.parse("rename file", ctx_fs())
    assert p2.needs_clarification
    assert "rename" in (p2.clarification_reason or "").lower()


def test_extract_routes_with_quoted_path():
    p = LP.parse('extract document "data/table.csv"', ctx_fs())
    assert p.needs_clarification is False
    assert p.capability == "filesystem.extract"
    assert p.parameters["path"] == "data/table.csv"
    p2 = LP.parse("document info", ctx_fs())
    assert p2.needs_clarification
    assert "document" in (p2.clarification_reason or "").lower() or \
        "file" in (p2.clarification_reason or "").lower()


# ---------------- browser ----------------

def test_find_on_page_routes_with_term():
    p = LP.parse('find on page "opening hours"', ctx_browser())
    assert p.capability == "browser.search"
    assert p.parameters["term"] == "opening hours"
    p2 = LP.parse("search page", ctx_browser())
    assert p2.needs_clarification
    assert "page" in (p2.clarification_reason or "").lower()


def test_links_and_form_route_param_free():
    for utterance, cap in (("list links", "browser.links"),
                           ("page links", "browser.links"),
                           ("read form", "browser.form"),
                           ("form fields", "browser.form"),
                           ("inspect form", "browser.form")):
        p = LP.parse(utterance, ctx_browser())
        assert p.needs_clarification is False
        assert p.capability == cap, utterance


def test_download_routes_url_and_name():
    p = LP.parse('download file https://example.com/files/notes.pdf '
                 '"notes.pdf"', ctx_browser())
    assert p.capability == "browser.download"
    assert p.parameters["url"] == "https://example.com/files/notes.pdf"
    assert p.parameters["name"] == "notes.pdf"


def test_download_requires_url_and_honest_name():
    p = LP.parse("download file", ctx_browser())
    assert p.needs_clarification
    assert "download" in (p.clarification_reason or "").lower()
    # Sensitive URL terminations are never silently renamed into the
    # download name: the request clarifies instead.
    p2 = LP.parse("download token.pem", ctx_browser())
    assert p2.needs_clarification
