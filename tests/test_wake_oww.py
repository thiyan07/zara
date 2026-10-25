"""OpenWakeWordSpotter (openWakeWord binding over onnxruntime) — unit
contracts. Honest failure is the priority: missing/corrupt model files
must raise descriptive errors, never silently return "no wake word".
Model-dependent tests SKIP cleanly when the trained files are absent,
like tests/test_wake_heyzara.py. No microphone is touched.
"""
import os

import pytest

from core.voice import (OpenWakeWordConfig, OpenWakeWordSpotter,
                        _spotter_from_env, wake_listen_once)

MODEL = os.path.expanduser("~/.cache/zara-wake/hey_zara.onnx")
needs_model = pytest.mark.skipif(
    not (os.path.isfile(MODEL)
         and os.path.isfile(os.path.expanduser(
             "~/.cache/zara-wake/oww/melspectrogram.onnx"))
         and os.path.isfile(os.path.expanduser(
             "~/.cache/zara-wake/oww/embedding_model.onnx"))),
    reason="hey_zara model + openWakeWord frontends not present")


class _Forced(OpenWakeWordSpotter):
    """Policy-only stub: forces per-call scores without any inference."""

    def __init__(self, config=None, forced=1.0, **kw):
        super().__init__(config, **kw)
        self._forced = forced

    def _load(self):
        return {}

    def _stream(self, x):
        return self.CHUNK

    def _score_call(self, processed):
        return self._forced


def _cfg(tmp_path, **kw):
    return OpenWakeWordConfig(
        model=str(tmp_path / "missing_knowledge.onnx"),
        frontend_dir=str(tmp_path / "missing_front"), **kw)


# ---------- config ----------

def test_config_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKE_OWW_MODEL", str(tmp_path / "m.onnx"))
    monkeypatch.setenv("WAKE_OWW_THRESHOLD", "0.7")
    monkeypatch.setenv("WAKE_OWW_CONS_FRAMES", "3")
    monkeypatch.setenv("WAKE_OWW_COOLDOWN_S", "2.5")
    cfg = OpenWakeWordConfig.from_env()
    assert cfg.model == str(tmp_path / "m.onnx")
    assert cfg.threshold == 0.7 and cfg.cons_frames == 3
    assert cfg.cooldown_s == 2.5


def test_config_bad_numeric_falls_back(monkeypatch):
    monkeypatch.setenv("WAKE_OWW_THRESHOLD", "not-a-number")
    cfg = OpenWakeWordConfig.from_env()
    assert cfg.threshold == OpenWakeWordConfig.threshold


# ---------- honest failure ----------

def test_missing_files_raise_descriptive(tmp_path):
    sp = OpenWakeWordSpotter(_cfg(tmp_path))
    with pytest.raises(RuntimeError, match="missing|not found"):
        sp.spot(b"\x00\x00" * 1600)


def test_empty_and_oversize_snippets_raise(tmp_path):
    sp = OpenWakeWordSpotter(_cfg(tmp_path, max_snippet_s=1.0))
    with pytest.raises(ValueError, match="empty"):
        sp.spot(b"")
    with pytest.raises(ValueError, match="exceeds"):
        sp.spot(b"\x00\x00" * (16000 * 3))


def test_snippet_cap_checked_before_model(tmp_path):
    # cap errors must never touch the microphone or the model dir
    sp = OpenWakeWordSpotter(_cfg(tmp_path))
    with pytest.raises(ValueError):
        sp.spot(b"\x00\x00" * (16000 * 11 * 2))


# ---------- detection policy (forced scores, no model needed) ----------

def test_warmup_suppresses_first_calls(tmp_path):
    sp = _Forced(_cfg(tmp_path), forced=1.0)
    one_s = b"\x00\x00" * 16000
    for _ in range(OpenWakeWordSpotter.WARMUP_CALLS):
        assert sp.spot(one_s)["detected"] is False
    assert sp.spot(one_s)["detected"] is False   # cons 1 < cons_frames 2
    out = sp.spot(one_s)  # cons 2 -> fires
    assert out["detected"] is True and out["confidence"] == 1.0


def test_consecutive_confirmation_required(tmp_path):
    sp = _Forced(_cfg(_make_plain(tmp_path), threshold=0.5,
                      cons_frames=3), forced=1.0)
    one_s = b"\x00\x00" * 16000
    for _ in range(OpenWakeWordSpotter.WARMUP_CALLS):
        assert sp.spot(one_s)["detected"] is False   # warm-up
    assert sp.spot(one_s)["detected"] is False  # cons 1
    assert sp.spot(one_s)["detected"] is False  # cons 2
    assert sp.spot(one_s)["detected"] is True   # cons 3 -> fire


def test_cooldown_suppresses_signal(tmp_path):
    sp = _Forced(_cfg(tmp_path, cooldown_s=60.0), forced=1.0)
    one_s = b"\x00\x00" * 16000
    for _ in range(5 + 2 + 1):  # warm-up (5) + cons ramp (2) + fire
        sp.spot(one_s)
    out = sp.spot(one_s)        # cons 2 again, inside the 60 s cooldown
    assert out["detected"] is False and out["cooldown"] is True


def test_below_threshold_never_confirms(tmp_path):
    sp = _Forced(_cfg(tmp_path), forced=0.2)
    for _ in range(20):
        out = sp.spot(b"\x00\x00" * 16000)
        assert out["detected"] is False and out["cooldown"] is False


def test_score_at_threshold_counts(tmp_path):
    # A score exactly at the threshold is a hit (>= gate), post warm-up.
    sp = _Forced(_cfg(tmp_path, threshold=0.1), forced=0.1)
    one_s = b"\x00\x00" * 16000
    for _ in range(OpenWakeWordSpotter.WARMUP_CALLS + 1):
        sp.spot(one_s)              # warm-up, then cons 1
    out = sp.spot(one_s)            # cons 2 -> fire
    assert out["detected"] is True and out["confidence"] == 0.1


def _make_plain(tmp_path):
    import itertools
    p = tmp_path / f"cfg{next(itertools.count())}"
    p.mkdir()
    return p


# ---------- engine selection ----------

def test_engine_selection_unknown_raises(monkeypatch):
    monkeypatch.setenv("ZARA_WAKE_ENGINE", "appium")
    with pytest.raises(ValueError, match="unknown ZARA_WAKE_ENGINE"):
        _spotter_from_env()


def test_engine_selection_explicit(monkeypatch):
    from core.voice import OnnxWakeSpotter
    monkeypatch.setenv("ZARA_WAKE_ENGINE", "onnx")
    assert isinstance(_spotter_from_env(), OnnxWakeSpotter)
    monkeypatch.setenv("ZARA_WAKE_ENGINE", "oww")
    assert isinstance(_spotter_from_env(), OpenWakeWordSpotter)


def test_engine_auto_falls_back_without_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ZARA_WAKE_ENGINE", "")
    monkeypatch.setenv("WAKE_OWW_MODEL", str(tmp_path / "none.onnx"))
    monkeypatch.setenv("WAKE_OWW_FRONTENDS", str(tmp_path))
    from core.voice import OnnxWakeSpotter
    assert isinstance(_spotter_from_env(), OnnxWakeSpotter)


# ---------- availability ----------

def test_availability_honest_without_files(tmp_path):
    sp = OpenWakeWordSpotter(_cfg(tmp_path))
    a = sp.availability()
    assert a["ready"] is False and a["always_on_dsp"] is False
    assert a["files"]["keyword_model"] is False
    assert "downloaded automatically" in a["note"]


@needs_model
def test_availability_ready_with_files():
    sp = OpenWakeWordSpotter()
    a = sp.availability()
    assert a["ready"] is True
    assert all(a["files"].values())
    assert "recorded" in a["note"]


# ---------- model-present inference (heaviest, skipped without files) ----------

@needs_model
def test_warmup_then_silence_stays_silent():
    import numpy as np
    sp = OpenWakeWordSpotter(OpenWakeWordConfig(threshold=0.5))
    silence = (np.zeros(16000, dtype=np.int16)).tobytes()
    confs = [sp.spot(silence)["confidence"] for _ in range(8)]
    assert all(c == 0.0 for c in
               confs[:OpenWakeWordSpotter.WARMUP_CALLS])
    assert max(confs[OpenWakeWordSpotter.WARMUP_CALLS:]) < 0.5


@needs_model
def test_wake_listen_once_missing_explicit_spotter_errors():
    cfg = OpenWakeWordConfig(
        model="/nonexistent/hey_zara.onnx", frontend_dir="/nonexistent")
    with pytest.raises(RuntimeError, match="missing|not found"):
        wake_listen_once(OpenWakeWordSpotter(cfg), pcm=b"\x00\x00" * 1600)
