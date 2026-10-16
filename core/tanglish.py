"""Deterministic Tanglish / Romanized-Tamil normalization (no dependencies).

Translates common Tamil (Tamil-script and Romanized) command words into
the English keywords the intent alias table already matches, so one
parser serves English, Tamil-script, and mixed input identically.
Conservative by design:

- The original text is always preserved (proposals carry it verbatim).
- URLs, filenames, quoted strings, and code-like identifiers are masked
  before mapping and restored after, so English product names, files,
  and technical terms are never corrupted.
- Ambiguous or unmapped input passes through unchanged; the parser then
  clarifies exactly as it would for equivalent English (parity, never
  silent invention of a destructive or sensitive command).
- Word order is normalized only for "<device> on|off" switches, which
  the alias table requires as "turn on|off <device>".
- This module never authorizes, approves, or executes anything.

Not complete Tamil understanding: a small curated dictionary plus
contextual rules. Anything outside it falls back to clarification.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# Tamil Unicode block: presence means Tamil-script input.
_TAMIL_RE = re.compile(r"[\u0b80-\u0bff]")

# Spans that must never be rewritten (same classes as intent.py's guards).
_URL_RE = re.compile(r"https?://[^\s\"'<>]{1,200}", re.I)
_FILENAME_RE = re.compile(
    r"(?:^|[\s\"'])((?:[\w\-. ]{1,80})\.(?:[\w]{1,10}))(?:[\s\"']|$)")
_QUOTED_RE = re.compile(r"[\"']([^\"']{1,120})[\"']")
# Code-like identifiers: camelCase, snake_case, dotted paths, digits-mixed.
_IDENT_RE = re.compile(
    r"\b(?:[A-Za-z]+[_.][A-Za-z0-9_.]+|[a-z]+[A-Z][A-Za-z0-9]*|"
    r"[A-Za-z]*\d[A-Za-z0-9_]*)\b")

# Minimal Tamil-script keyword map (common command words only).
TA_SCRIPT_MAP: dict[str, str] = {
    "ஆன்": "on",
    "ஆஃப்": "off",
    "நிலை": "status",
    "பேட்டரி": "battery",
    "திற": "open",
    "மூடு": "close",
    "என்ன": "what",
    "எவ்வளவு": "how much",
    "இந்த": "this",
    "அந்த": "that",
    "செய்": "",
    "பண்ணு": "",
    "சொல்லு": "",
    "இருக்கு": "is",
    "இருக்கிறது": "is",
}

# Romanized Tamil -> English keyword. "" drops light verbs/particles so
# the content words (which the alias table matches) remain.
# Order matters only for multiword entries; all match whole words.
ROMAN_MAP: list[tuple[str, str]] = [
    # Multiword first.
    ("how many", "how much"),  # canonicalize before Tamil insertions
    # Question words.
    ("ennana", "what"), ("enna", "what"),
    ("evvalavu", "how much"), ("evloo", "how much"), ("evlo", "how much"),
    ("eppadi", "how"), ("epdi", "how"),
    ("yaaru", "who"), ("yaar", "who"),
    ("enge", "where"), ("eppo", "when"), ("edhukku", "why"),
    ("irukka", "is there"), ("irukkuma", "is there"),
    # Pronouns / demonstratives.
    ("indha", "this"), ("inda", "this"), ("idhu", "this"),
    ("andha", "that"), ("adhu", "that"),
    ("enoda", "my"), ("enakku", "for me"), ("enakaga", "for me"),
    ("unakku", "for you"), ("naan", "i"), ("nee", "you"),
    # State / existence.
    ("irukku", "is"), ("iruku", "is"), ("irukken", "am"),
    ("illai", "not"), ("illa", "not"),
    # Light verbs and particles (dropped; action nouns carry intent).
    ("pannunga", ""), ("pannuda", ""), ("pannu", ""),
    ("panreenga", ""), ("pandra", ""), ("panra", ""),
    ("seyyunga", ""), ("seyyu", ""), ("sei", ""),
    ("sollunga", ""), ("solluda", ""), ("sollu", ""),
    ("kodu", "give"), ("kelu", "ask"),
    ("paaru", "show"), ("paarunga", "show"),
    # Switch verbs.
    ("podu", "on"),
]

# "<device> on|off" (SOV order) -> "turn on|off <device>" (alias order).
_SWITCH_RE = re.compile(
    r"\b(wifi|wi-fi|bluetooth|hotspot|mobile data|airplane mode|"
    r"flight mode) (on|off)\b")

# Standalone Tamil particles, dropped only when another Tamil marker
# already mapped in the same text (gated second pass).
_PARTICLE_RE = re.compile(r"\bla\b|\bda\b|\bdi\b|\bna\b|\bma\b")


@dataclass
class Normalization:
    """Structured result: original preserved, normalized for matching."""
    original: str
    normalized: str
    language: str = "en"  # en | ta | tanglish
    confidence: float = 1.0
    mapped: bool = False
    notes: list[str] = field(default_factory=list)


def _mask_spans(text: str) -> tuple[str, list[str]]:
    """Replace protected spans with placeholders; return masked + originals.

    Single pass over all patterns: longest match wins at each position
    and overlaps are dropped, so placeholders can never be re-matched
    by a later pattern (which previously corrupted them).
    """
    patterns = (_URL_RE, _FILENAME_RE, _QUOTED_RE, _IDENT_RE)
    hits: list[tuple[int, int, str]] = []
    for rx in patterns:
        for m in rx.finditer(text):
            hits.append((m.start(), m.end(), m.group(0)))
    # Longest-first at each start; skip anything overlapping a kept span.
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    saved: list[str] = []
    out: list[str] = []
    pos = 0
    for start, end, orig in hits:
        if start < pos:
            continue
        out.append(text[pos:start])
        saved.append(orig)
        out.append(f"\x00{len(saved) - 1}\x00")
        pos = end
    out.append(text[pos:])
    return "".join(out), saved


def _unmask(text: str, saved: list[str]) -> str:
    for i, orig in enumerate(saved):
        text = text.replace(f"\x00{i}\x00", orig)
    return text


def _apply_word_map(text: str) -> tuple[str, bool]:
    mapped = False
    for src, dst in ROMAN_MAP:
        new, n = re.subn(r"\b" + re.escape(src) + r"\b", dst, text)
        if n:
            mapped = True
            text = new
    # Collapse gaps left by dropped words (keep single spaces).
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text, mapped


def normalize_utterance(text: object) -> Normalization:
    """Normalize Tamil-script / Tanglish input to matchable English.

    Never raises; never invents authority. Returns the original plus
    the normalized form, detected language, and a mapping flag.
    """
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # noqa: BLE001
            return Normalization(original="", normalized="",
                                 language="en", confidence=1.0)
    original = text
    has_tamil_script = bool(_TAMIL_RE.search(text))
    masked, saved = _mask_spans(text)
    work = masked.lower()
    notes: list[str] = []
    mapped = False

    if has_tamil_script:
        lowered = masked.lower()
        for src, dst in TA_SCRIPT_MAP.items():
            if src in lowered:
                mapped = True
                lowered = lowered.replace(src, dst)
        work = lowered

    work, roman_mapped = _apply_word_map(work)
    mapped = mapped or roman_mapped

    if mapped:
        # Second pass: drop standalone particles only when Tamil markers
        # already mapped (never in pure English text).
        work, _ = _apply_word_map_particles(work)

    # Device switch reorder (alias table needs SVO order).
    work, n_sw = _SWITCH_RE.subn(
        lambda m: f"turn {m.group(2)} {m.group(1)}", work)
    if n_sw:
        mapped = True

    normalized = _unmask(work, saved).strip()
    normalized = re.sub(r"\s{2,}", " ", normalized)
    if len(normalized) > 2000:
        normalized = normalized[:2000]

    if has_tamil_script and mapped:
        language, confidence = "ta", 0.8
    elif has_tamil_script:
        language, confidence = "ta", 0.5
        notes.append("tamil-script input with no mapped keywords; "
                     "parser will clarify")
    elif mapped:
        language, confidence = "tanglish", 0.8
    else:
        language, confidence = "en", 1.0
    return Normalization(original=original, normalized=normalized,
                         language=language, confidence=confidence,
                         mapped=mapped, notes=notes)


def _apply_word_map_particles(text: str) -> tuple[str, bool]:
    new, n = _PARTICLE_RE.subn(" ", text)
    new = re.sub(r"\s{2,}", " ", new).strip()
    return new, n > 0
