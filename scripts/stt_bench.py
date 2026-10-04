"""STT benchmark: faster-whisper tiny.en vs NVIDIA Parakeet (if configured).

Fixtures: tests/fixtures/stt/*.wav — project-owned Piper synthesis
(en_US-lessac-medium), 16 kHz mono. Reference transcripts live in
REFERENCE below. No private recordings, ever.

Honest limits (see docs/STT_STATUS.md):
- Accent/noise/fast-speech/Tamil categories are NOT covered by these
  fixtures (single American-English TTS voice, clean synthesis).
  They are marked NOT_TESTED, never inferred.
- Indian English / noise / short commands on REAL hardware are covered
  by physical validation on vivo V2338, not by this harness.

Usage:
  python3 scripts/stt_bench.py [--providers local,nvidia] [--repeat 1]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_dotenv():
    # Minimal .env loader (no new dependency): KEY=value lines only.
    # Never prints values.
    root = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(root) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_dotenv()

FIXDIR = os.path.join(os.path.dirname(__file__), "..",
                      "tests", "fixtures", "stt")
REFERENCE = {
    "battery": "What is my battery level?",
    "tech": "Check FastAPI and PostgreSQL status.",
    "numbers": "Postpone the meeting to 5 PM on room 204.",
    "filename": "Create a Python file called test agent.",
    "short": "Run the tests.",
}


def norm(s):
    import re as _re
    return _re.sub(r"[^a-z0-9 ]", "", s.lower()).strip().split()


def wer(ref_words, hyp_words):
    # Simple Levenshtein WER; no new dependencies.
    import numpy as _np
    n, m = len(ref_words), len(hyp_words)
    d = _np.zeros((n + 1, m + 1), dtype=int)
    d[:, 0] = _np.arange(n + 1)
    d[0, :] = _np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (ref_words[i - 1] !=
                                             hyp_words[j - 1]))
    return float(d[n, m]) / max(1, n)


def build_providers(which):
    from core.voice import FasterWhisperSTT
    out = {}
    if "local" in which:
        out["local"] = FasterWhisperSTT()
    if "nvidia" in which:
        from core.stt_nvidia import NvidiaSTT
        try:
            p = NvidiaSTT()
            a = p.availability()
            if not (a["configured"] and a["transport"]):
                print("nvidia: NOT_CONFIGURED (key or riva-client missing)")
            else:
                out["nvidia"] = p
        except Exception as e:  # noqa: BLE001
            print(f"nvidia: unavailable ({type(e).__name__})")
    return out


def main():
    args = sys.argv[1:]
    providers = ["local", "nvidia"]
    repeat = 1
    for a in args:
        if a.startswith("--providers="):
            providers = a.split("=", 1)[1].split(",")
        if a.startswith("--repeat="):
            repeat = max(1, int(a.split("=", 1)[1]))
    provs = build_providers(providers)
    if not provs:
        print("no providers available")
        return 1
    files = sorted(f for f in os.listdir(FIXDIR) if f.endswith(".wav"))
    rows = []
    for fname in files:
        name = fname[:-4]
        ref = REFERENCE.get(name, "")
        with open(os.path.join(FIXDIR, fname), "rb") as f:
            audio = f.read()
        for pname, p in provs.items():
            best = None
            for _ in range(repeat):
                t = time.monotonic()
                try:
                    hyp = p.transcribe(audio)
                    err = ""
                except Exception as e:  # noqa: BLE001
                    hyp, err = "", f"{type(e).__name__}"
                dt = time.monotonic() - t
                w = wer(norm(ref), norm(hyp)) if hyp else 1.0
                if best is None or w < best["wer"]:
                    best = {"provider": pname, "fixture": name,
                            "latency_s": round(dt, 2),
                            "wer": round(w, 3),
                            "exact": norm(ref) == norm(hyp),
                            "hypothesis": hyp, "error": err,
                            "bytes": len(audio)}
            rows.append(best)
            print(json.dumps(best))
    ok = [r for r in rows if not r["error"]]
    if ok:
        for p in provs:
            pr = [r for r in ok if r["provider"] == p]
            if pr:
                print(f"== {p}: mean_latency="
                      f"{sum(r['latency_s'] for r in pr)/len(pr):.2f}s "
                      f"mean_wer={sum(r['wer'] for r in pr)/len(pr):.3f} "
                      f"exact={sum(r['exact'] for r in pr)}/{len(pr)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
