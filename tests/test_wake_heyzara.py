"""Stage 3 validation contracts for the hey_zara model (Stage 4 gates).

Model-dependent tests SKIP cleanly when the model file is absent —
they must never fail-closed-block CI, and must never claim success
without test evidence. Pure-function tests (threshold sweep, leakage,
WAV loader, lifecycle defaults) always run.
"""
import json
import os

import pytest

MODEL = os.path.expanduser("~/.cache/zara-wake/hey_zara.onnx")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS = os.path.join(REPO, "training", "hey_zara_metrics.json")

needs_model = pytest.mark.skipif(
    not os.path.isfile(MODEL), reason="hey_zara model not trained yet")


def test_wav_loader_rejects_bad_format(tmp_path):
    import wave
    from training.validate_heyzara import load_wav_16k
    bad = tmp_path / "bad.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00" * 100)
    with pytest.raises(ValueError, match="16-bit 16 kHz"):
        load_wav_16k(str(bad))


def test_wav_loader_loads_mono_16k(tmp_path):
    import wave
    import numpy as np
    from training.validate_heyzara import load_wav_16k
    good = tmp_path / "good.wav"
    with wave.open(str(good), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.zeros(1600, dtype=np.int16)).tobytes())
    assert load_wav_16k(str(good)).shape == (1600,)


def test_leakage_check_fires_on_identical_bytes(tmp_path):
    from training.validate_heyzara import check_leakage
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(b"RIFFxxxx")
    b.write_bytes(b"RIFFxxxx")  # identical content, different names
    with pytest.raises(ValueError, match="split leakage"):
        check_leakage([str(a)], [str(b)])
    c = tmp_path / "c.wav"
    c.write_bytes(b"RIFFyyyy")
    check_leakage([str(a)], [str(c)])  # no raise
    # Same basename, different bytes: expected layout, no raise.
    d = tmp_path / "sub" / "a.wav"
    d.parent.mkdir()
    d.write_bytes(b"RIFFzzzz")
    check_leakage([str(a)], [str(d)])


def test_threshold_sweep_selects_measured_not_guessed():
    from training.validate_heyzara import sweep_thresholds
    pos = [0.9, 0.8, 0.7, 0.6]
    neg = [0.1, 0.2, 0.55, 0.3]
    best = sweep_thresholds(pos, neg, neg_hours=2.0)
    assert best["threshold"] == 0.35  # lowest thr with FP<=0.5/hr
    assert best["recall"] == 1.0
    assert best["fp_per_hour"] <= 0.5


def test_silence_scores_below_threshold_without_model():
    # Silence can never be a positive: guard the metric logic even
    # before any model exists (silence -> 0.0 scores -> no fire).
    from training.validate_heyzara import sweep_thresholds
    best = sweep_thresholds([0.0, 0.0], [0.0] * 10, neg_hours=1.0)
    assert best["fp"] == 0


def test_listen_session_off_by_default_no_model_needed():
    from core.voice import WakeListenSession
    sess = WakeListenSession()
    assert sess.state == "off"
    assert sess.availability()["always_on_dsp"] is False


@needs_model
def test_model_present_nonempty_loadable_cpu():
    import onnxruntime as ort
    size = os.path.getsize(MODEL)
    assert size > 10000, f"suspiciously small: {size}"
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    sess = ort.InferenceSession(MODEL, opts,
                                providers=["CPUExecutionProvider"])
    assert len(sess.get_inputs()) >= 1


@needs_model
def test_reported_metrics_are_measured_and_honest():
    assert os.path.isfile(METRICS), "run training/validate_heyzara.py first"
    m = json.load(open(METRICS))
    assert m["n_pos"] >= 50 and m["n_neg"] >= 50
    # Structural honesty, not an aspirational bar: the threshold must
    # come from the sweep, scores must be real fractions, and the
    # synthetic-only limitation must be declared. Whether the bar
    # (recall>=0.9, FP<=0.5/hr) is met is reported, never asserted.
    assert "threshold" in m and "recall_at_0.5" in m
    assert 0.0 <= m["recall_at_0.5"] <= 1.0
    assert "synthetic-only" in m.get("limitation", "")
