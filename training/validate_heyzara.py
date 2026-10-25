"""Validation for the hey_zara openWakeWord model (Stage 3).

Usage (after training produces the model file):
    python training/validate_heyzara.py \
        --model ~/.cache/zara-wake/hey_zara.onnx \
        --pos-dir hey_zara_model/test_clips \
        --neg-dir hey_zara_model/neg_clips \
        --out training/hey_zara_metrics.json

The classifier consumes openWakeWord embedding features (mel +
embedding models from the openWakeWord release, ~2.4 MB total,
downloaded once at validation time with the integrity checks below —
NOT vendored here). Scores every clip in 1280-sample frames like the
streaming path, sweeps thresholds, and reports recall, false-activation
rate over the scored audio hours, and the threshold maximizing recall
subject to FP/hour <= 0.5. Writes metrics JSON (consumed by
tests/test_wake_heyzara.py). No audio persisted or uploaded; no
microphone use. Synthetic-only results must never be presented as
real-world accuracy.
"""
import argparse
import glob
import json
import os
import wave

import numpy as np

FEATURE_URLS = {
    "melspectrogram.onnx":
        "https://github.com/dscripka/openWakeWord/releases/download/"
        "v0.5.1/melspectrogram.onnx",
    "embedding_model.onnx":
        "https://github.com/dscripka/openWakeWord/releases/download/"
        "v0.5.1/embedding_model.onnx",
}
# Verified 2026-10-14 from the release API (v0.5.1 asset metadata).
FEATURE_SIZES = {
    "melspectrogram.onnx": 1087958,
    "embedding_model.onnx": 1326578,
}


def load_wav_16k(path):
    with wave.open(path, "rb") as w:
        if w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError(f"bad format (need 16-bit 16 kHz): {path}")
        raw = w.readframes(w.getnframes())
    ch = w.getnchannels()
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return pcm


def check_leakage(pos, neg, train_dirs=()):
    """Content-hash guard: no validation clip may be byte-identical to
    another validation clip on the opposite side, or to any training
    clip. Basename overlap (0.wav in every dir) is expected and ignored."""
    import hashlib

    def _hash(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
        return h.hexdigest()

    seen = {}
    for p in pos:
        seen.setdefault(_hash(p), []).append(("pos", p))
    for p in neg:
        seen.setdefault(_hash(p), []).append(("neg", p))
    bad = {h: v for h, v in seen.items()
           if {s for s, _ in v} == {"pos", "neg"}}
    if bad:
        sample = sorted({p for v in bad.values() for _, p in v})[:5]
        raise ValueError(f"split leakage (identical bytes): {sample}")
    if train_dirs:
        val_hashes = set(seen)
        dupes = []
        for d in train_dirs:
            if not os.path.isdir(d):
                continue
            for root, _, files in os.walk(d):
                for fn in files:
                    if not fn.endswith(".wav"):
                        continue
                    p = os.path.join(root, fn)
                    if _hash(p) in val_hashes:
                        dupes.append(p)
                        if len(dupes) >= 5:
                            break
        if dupes:
            raise ValueError(
                f"train/val content overlap: {sorted(dupes)[:5]}")


def sweep_thresholds(pos_scores, neg_scores, neg_hours, fp_cap=0.5):
    best = None
    for i in range(4, 20):
        thr = round(i * 0.05, 2)
        recall = sum(s >= thr for s in pos_scores) / len(pos_scores)
        fp = sum(s >= thr for s in neg_scores)
        fp_per_hour = fp / neg_hours if neg_hours > 0 else float("inf")
        if fp_per_hour <= fp_cap and (best is None or recall > best["recall"]):
            best = {"threshold": thr, "recall": recall,
                    "fp": fp, "fp_per_hour": fp_per_hour}
    # Always report the 0.5 operating point too, so a missed bar still
    # yields measured numbers instead of zeros.
    r05 = sum(s >= 0.5 for s in pos_scores) / len(pos_scores)
    f05 = sum(s >= 0.5 for s in neg_scores)
    base = {"recall_at_0.5": r05, "fp_at_0.5": f05,
            "fp_per_hour_at_0.5": f05 / neg_hours if neg_hours > 0
            else float("inf")}
    if best is None:
        best = {"threshold": 0.5, "recall": r05, "fp": f05,
                "fp_per_hour": base["fp_per_hour_at_0.5"],
                "note": "no threshold met FP<=0.5/hr"}
    return {**base, **best}


def ensure_feature_models(cache_dir):
    """Shared mel+embedding models must already exist (downloaded once
    during setup with size checks). Refuses truncated files; never
    re-downloads silently here."""
    paths = {}
    for name, size in FEATURE_SIZES.items():
        dest = os.path.join(cache_dir, name)
        if not os.path.isfile(dest) or os.path.getsize(dest) != size:
            raise ValueError(
                f"{name}: missing or size mismatch (expected {size}); "
                f"fetch it with the setup commands first")
    import sys
    for cand in (os.path.expanduser("~/.cache/zara-wake-train/models"),
                 cache_dir):
        paths[cand] = True
    return (os.path.join(os.path.expanduser("~/.cache/zara-wake-train/models"),
                         "melspectrogram.onnx"),
            os.path.join(os.path.expanduser("~/.cache/zara-wake-train/models"),
                         "embedding_model.onnx"))


def load_oww_model(clf_path, mel_path, emb_path):
    """Exact upstream streaming path (Apache-2.0, runtime import — not
    vendored): openwakeword.Model with our classifier, onnx framework,
    single-threaded sessions inside. Falls back to the training
    checkout when the pip package is absent."""
    import sys
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        src = os.path.expanduser(
            "~/.cache/zara-wake-train/src/openWakeWord")
        if not os.path.isdir(src):
            raise RuntimeError(
                "openwakeword unavailable: pip install openwakeword or "
                "provide the training checkout")
        sys.path.insert(0, src)
        import openwakeword  # noqa: F401
    from openwakeword.model import Model
    return Model(wakeword_models=[clf_path], inference_framework="onnx",
                 melspec_model_path=mel_path,
                 embedding_model_path=emb_path)


def score_clip(model, pcm):
    """Max streaming score over 1280-sample chunks (the exact Model
    path, including its first-5-frames warmup zeroing). The pipeline
    consumes int16 samples; float input is clipped and cast."""
    import numpy as np
    pcm16 = np.clip(pcm, -32768, 32767).astype(np.int16)
    model.reset()
    best = 0.0
    for i in range(0, max(len(pcm16) - 1280, 1), 1280):
        frame = pcm16[i:i + 1280]
        if len(frame) < 1280:
            frame = np.pad(frame, (0, 1280 - len(frame)))
        out = model.predict(frame)
        for v in out.values():
            best = max(best, float(v))
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--pos-dir", required=True)
    ap.add_argument("--neg-dir", required=True)
    ap.add_argument("--feature-cache",
                    default=os.path.expanduser("~/.cache/zara-wake/oww"))
    ap.add_argument("--out", default="training/hey_zara_metrics.json")
    ap.add_argument("--limitation",
                    default="synthetic-only clips; NOT real-world accuracy")
    args = ap.parse_args()

    import onnxruntime as ort

    size = os.path.getsize(args.model)
    assert size > 0, "model file empty"
    print(f"model: {args.model} ({size} bytes)")

    mel_path, emb_path = ensure_feature_models(args.feature_cache)
    model = load_oww_model(args.model, mel_path, emb_path)

    pos = sorted(glob.glob(os.path.join(args.pos_dir, "*.wav")))
    neg = sorted(glob.glob(os.path.join(args.neg_dir, "*.wav")))
    assert pos, "no positive clips"
    assert neg, "no negative clips"
    model_dir = os.path.dirname(args.pos_dir)
    check_leakage(pos, neg, train_dirs=[
        os.path.join(model_dir, "positive_train"),
        os.path.join(model_dir, "negative_train")])

    pos_scores = [score_clip(model, load_wav_16k(p)) for p in pos]
    neg_scores = [score_clip(model, load_wav_16k(p)) for p in neg]
    neg_hours = sum(os.path.getsize(p) for p in neg) / 32000.0 / 3600.0
    best = sweep_thresholds(pos_scores, neg_scores, neg_hours)

    metrics = {
        "model": os.path.basename(args.model),
        "model_bytes": size,
        "n_pos": len(pos), "n_neg": len(neg),
        "neg_hours": round(neg_hours, 3),
        **best,
        "limitation": args.limitation,
    }
    with open(args.out, "w") as f:
        json.dump(metrics, f, indent=2)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
