"""ONNX wake spotter: real tensor contract, cons-frames, cooldown, close.

All inference is faked via injected sessions shaped like the validated
upstream contract — mel frontend emits (1,1,F), classifier takes
(1,mel_time,n_mels) and emits [1,N] raw scores. No model files, no mic,
no downloads. Real detection stays unavailable until a trained
'Hey Zara' model exists (see docs/WAKE_ONNX_SETUP.md).
"""
import pytest

from core.voice import OnnxWakeConfig, OnnxWakeSpotter


class _In:
    def __init__(self, name="input", shape=()):
        self.name = name
        self.shape = shape


class _Session:
    """Fake ort session: run() returns queued outputs; records feeds."""

    def __init__(self, outputs, shape=()):
        self.outputs = list(outputs)
        self.calls = 0
        self.feeds = []
        self._shape = shape

    def get_inputs(self):
        return [_In(shape=self._shape)]

    def run(self, _, feeds):
        self.calls += 1
        self.feeds.append({k: v.shape for k, v in feeds.items()})
        return [self.outputs.pop(0)]


def _live(keyword="Hey Zara", scores=((0.1, 0.9),), threshold=0.4,
          cooldown_s=60.0, cons_frames=2, mel_frames=120):
    """Fake sessions: mel emits (1,1,mel_frames); classifier emits [1,2]
    raw scores per call. cons_frames=1 makes single calls decisive."""
    import numpy as _np
    cfg = OnnxWakeConfig(
        model_info="/nonexistent/model_info.json",
        keyword_model="/nonexistent/kw.onnx",
        mel_model="/nonexistent/mel.onnx",
        keyword_name=keyword, threshold=threshold, cooldown_s=cooldown_s)
    mel = _Session([_np.zeros((1, 1, mel_frames, 32), dtype=_np.float32)
                    for _ in scores], shape=("b", "c", "f", "m"))
    kws = _Session([_np.array([list(lg)], dtype=_np.float32)
                    for lg in scores], shape=(1, 98, 32))
    sp = OnnxWakeSpotter(cfg, _sessions={"mel": mel, "keyword": kws})
    sp._keywords = ["Other", keyword]
    sp._mel_time = 98
    sp._n_mels = 32
    sp._cons_frames = cons_frames
    return sp, mel, kws


def test_config_defaults_match_validated_contract():
    cfg = OnnxWakeConfig.from_env()
    assert cfg.keyword_name == "Hey Zara"
    assert cfg.threshold == 0.4  # upstream default
    assert cfg.cooldown_s == 1.5
    assert cfg.max_snippet_s == 5.0
    assert cfg.sample_rate == 16000


def test_missing_model_files_raise_honestly():
    sp = OnnxWakeSpotter(OnnxWakeConfig(
        model_info="/nonexistent/mi.json", keyword_model="/none/x.onnx",
        mel_model="/none/y.onnx"))
    with pytest.raises(RuntimeError, match="wake model missing"):
        sp.spot(b"\x00" * 320)


def test_empty_and_oversize_snippets_rejected():
    sp, _, _ = _live()
    with pytest.raises(ValueError, match="empty snippet"):
        sp.spot(b"")
    with pytest.raises(ValueError, match="exceeds"):
        sp.spot(b"\x00" * (16000 * 2 * 5 + 2))


def test_audio_window_padded_and_shaped():
    # 160-sample snippet pads to the 16080-sample window, reshaped
    # (1, N) for the mel frontend; classifier gets (1, 98, 32).
    sp, mel, kws = _live(scores=[(0.1, 0.2)], cons_frames=99)
    sp.spot(b"\x00" * 320)
    assert mel.feeds[0] == {"input": (1, 16080)}
    assert kws.feeds[0] == {"input": (1, 98, 32)}


def test_hit_needs_consecutive_frames_then_fires():
    sp, _, _ = _live(scores=[(0.1, 5.0), (0.1, 5.0)], cons_frames=2)
    first = sp.spot(b"\x00" * 320)
    assert first["detected"] is False and first["cooldown"] is False
    assert first["word"] == "Hey Zara"
    second = sp.spot(b"\x01" * 320)
    assert second["detected"] is True
    assert second["confidence"] > 0.9
    assert second["cons_frames"] == 2


def test_below_threshold_never_confirms():
    sp, _, _ = _live(scores=[(5.0, 0.1)] * 4, cons_frames=2)
    for _ in range(4):
        out = sp.spot(b"\x00" * 320)
        assert out["detected"] is False and out["cooldown"] is False


def test_wrong_keyword_never_counts_as_hey_zara():
    sp, _, _ = _live(keyword="Hey Zara",
                     scores=[(5.0, 0.1)] * 3, cons_frames=2)
    for _ in range(3):
        assert sp.spot(b"\x00" * 320)["detected"] is False


def test_miss_resets_consecutive_count():
    sp, _, _ = _live(scores=[(0.1, 5.0), (5.0, 0.1), (0.1, 5.0),
                             (0.1, 5.0)], cons_frames=2)
    assert sp.spot(b"\x00" * 320)["detected"] is False  # cons=1
    assert sp.spot(b"\x00" * 320)["detected"] is False  # miss resets
    assert sp.spot(b"\x00" * 320)["detected"] is False  # cons=1 again
    assert sp.spot(b"\x00" * 320)["detected"] is True  # cons=2 fires


def test_cooldown_suppresses_repeat_events():
    sp, _, _ = _live(scores=[(0.1, 5.0)] * 4, cons_frames=1,
                     cooldown_s=60.0)
    assert sp.spot(b"\x00" * 320)["detected"] is True
    second = sp.spot(b"\x01" * 320)
    assert second == {"detected": False, "word": "Hey Zara",
                      "confidence": pytest.approx(second["confidence"]),
                      "cons_frames": 1, "cooldown": True}


def test_close_is_idempotent_and_drops_sessions():
    sp, _, _ = _live()
    sp.close()
    sp.close()
    assert sp._sessions is None and sp._keywords == []


def test_availability_reports_unready_without_files():
    sp = OnnxWakeSpotter(OnnxWakeConfig(
        model_info="/nonexistent/mi.json", keyword_model="/none/x.onnx",
        mel_model="/none/y.onnx"))
    av = sp.availability()
    assert av["ready"] is False
    assert av["always_on_dsp"] is False
    assert av["physically_validated"] is False
    assert "Hey Zara" in av["note"]


def test_no_audio_retained_after_spot():
    sp, _, _ = _live(cons_frames=99)
    sp.spot(b"\x02" * 640)
    assert not any(getattr(v, "tobytes", lambda: b"")() == b"\x02" * 640
                   for v in vars(sp).values())


def _cfg():
    return OnnxWakeConfig(
        model_info="/nonexistent/model_info.json",
        keyword_model="/nonexistent/kw.onnx",
        mel_model="/nonexistent/mel.onnx")


def test_capture_missing_binary_raises(monkeypatch):
    import shutil
    from core import voice as _v
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="arecord not installed"):
        _v.capture_snippet_alsa()


def test_capture_rejects_bad_params():
    from core import voice as _v
    with pytest.raises(ValueError, match="1.0..5.0"):
        _v.capture_snippet_alsa(seconds=30.0)
    with pytest.raises(ValueError, match="16000"):
        _v.capture_snippet_alsa(rate=44100)


def test_capture_arecord_error_and_timeout(monkeypatch):
    import subprocess as _sp
    from core import voice as _v

    class R:
        returncode = 1
        stdout = b""
        stderr = b"ALSA: no such device"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="no microphone audio"):
        _v.capture_snippet_alsa()

    def boom(*a, **k):
        raise _sp.TimeoutExpired(cmd="arecord", timeout=1)

    monkeypatch.setattr(_sp, "run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        _v.capture_snippet_alsa()


def test_listen_once_validates_model_before_mic(monkeypatch):
    # Missing model must fail WITHOUT invoking arecord (no room
    # recording spent on a doomed run).
    import subprocess as _sp
    from core import voice as _v
    called = []

    def spy(*a, **k):
        called.append(True)
        raise AssertionError("mic touched despite missing model")

    monkeypatch.setattr(_sp, "run", spy)
    sp = OnnxWakeSpotter(_cfg())
    with pytest.raises(RuntimeError, match="wake model missing"):
        _v.wake_listen_once(sp)
    assert called == []


def test_listen_once_injected_pcm_scores_without_capture(monkeypatch):
    import subprocess as _sp
    from core import voice as _v
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("capture must not run")))
    sp, _, _ = _live(scores=[(0.1, 5.0)], cons_frames=1)
    out = _v.wake_listen_once(sp, pcm=b"\x00" * 320)
    assert out["detected"] is True and out["word"] == "Hey Zara"


# ---------- WakeListenSession: single owner, prompt stop, honest errors ----------

class _FakeStream:
    """Canned PCM source. `chunks`: bytes returned per read (cycled);
    `fail_after`: reads before raising (device failure simulation)."""

    def __init__(self, chunks=(b"\x00" * 32000,), fail_after=-1):
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.reads = 0
        self.closed = False

    def read(self, n):
        self.reads += 1
        if self.fail_after >= 0 and self.reads > self.fail_after:
            raise OSError("device disconnected")
        out = self.chunks[(self.reads - 1) % len(self.chunks)]
        return out[:n] if len(out) >= n else out + b"\x00" * (n - len(out))

    def close(self):
        self.closed = True


class _StubSpotter:
    """Canned inference: results queued; records calls; never a model."""

    def __init__(self, results=(), fail=None):
        self.results = list(results)
        self.fail = fail
        self.calls = 0

    def _load(self):
        if self.fail == "load":
            raise RuntimeError("wake model missing: stub")

    def spot(self, pcm):
        self.calls += 1
        if self.fail == "spot":
            raise RuntimeError("inference exploded")
        if self.results:
            return self.results.pop(0)
        return {"detected": False, "word": "", "confidence": 0.0,
                "cons_frames": 2, "cooldown": False}

    def availability(self):
        return {"engine": "stub", "ready": True}


def _session(stream=None, spotter=None, events=None, **kw):
    from core.voice import WakeListenSession
    got = [] if events is None else events
    args = dict(spotter=spotter or _StubSpotter(), on_event=got.append,
                window_s=0.05, max_retries=2, retry_backoff_s=0.01)
    args.update(kw)
    if stream is not None:
        args["spawn"] = lambda: stream
    return WakeListenSession(**args), got


def _wait_for(cond, timeout_s=5.0):
    import time as _t
    end = _t.time() + timeout_s
    while _t.time() < end:
        if cond():
            return True
        _t.sleep(0.01)
    return cond()


def test_session_off_by_default_and_model_before_mic():
    sess, _ = _session(spotter=_StubSpotter(fail="load"),
                       stream=_FakeStream())
    assert sess.state == "off"
    assert sess.start() is False  # model fails: mic never opened
    assert sess.state == "error"
    assert "wake model missing" in sess.error


def test_session_single_owner_and_event_delivery():
    stream = _FakeStream()
    sess, got = _session(
        stream=stream,
        spotter=_StubSpotter(results=[
            {"detected": True, "word": "Hey Zara", "confidence": 0.9,
             "cons_frames": 2, "cooldown": False}] * 3))
    assert sess.start() is True
    assert sess.start() is False  # duplicate refused
    assert _wait_for(lambda: len(got) >= 1)
    assert got[0]["word"] == "Hey Zara"
    assert sess.state == "listening" and sess.windows >= 1
    sess.stop()
    assert sess.state == "off"
    assert stream.closed is True
    assert sess._thread is None


def test_session_repeated_on_off_transitions():
    sess, _ = _session(stream=_FakeStream())
    for _ in range(3):
        assert sess.start() is True
        assert _wait_for(lambda: sess.windows >= 1)
        sess.stop()
        assert sess.state == "off"
    sess.stop()  # idempotent
    assert sess.state == "off"


def test_session_stop_drops_late_results():
    sess, got = _session(stream=_FakeStream())
    assert sess.start() is True
    assert _wait_for(lambda: sess.windows >= 1)
    sess.stop()
    assert sess.state == "off"
    stale_gen = sess._generation - 1  # the generation that just ended
    assert stale_gen >= 0
    # Simulate a racing worker iteration from the dead generation: it
    # may still read+score, but must never deliver the event.
    sess._stream = _FakeStream()
    before = len(got)
    sess._loop(stale_gen)
    assert len(got) == before
    sess._stream = None


def test_session_capture_failure_parks_at_error_bounded():
    stream = _FakeStream(fail_after=0)
    sess, got = _session(stream=stream, max_retries=2,
                         retry_backoff_s=0.01)
    assert sess.start() is True
    assert _wait_for(lambda: sess.state == "error")
    assert "disconnected" in sess.error
    assert sess.retries == 3  # initial + max_retries, then parked
    assert got == []  # failures never emit events
    sess.stop()
    assert sess.state == "off"


def test_session_spot_failure_retries_then_parks():
    sess, _ = _session(stream=_FakeStream(),
                       spotter=_StubSpotter(fail="spot"),
                       max_retries=1, retry_backoff_s=0.01)
    assert sess.start() is True
    assert _wait_for(lambda: sess.state == "error")
    assert "inference exploded" in sess.error
    sess.stop()


def test_session_shutdown_leaves_no_worker():
    sess, _ = _session(stream=_FakeStream())
    assert sess.start() is True
    thread = sess._thread
    sess.stop()
    assert not thread.is_alive()
    assert sess._thread is None


def test_session_availability_reports_state():
    sess, _ = _session()
    av = sess.availability()
    assert av["state"] == "off" and av["always_on_dsp"] is False
    assert "capture" in av


def test_live_bogus_device_fails_without_recording():
    # Real arecord binary, impossible device: ALSA errors immediately,
    # zero audio captured, bounded time. Proves graceful handling.
    import time as _t
    from core.voice import WakeListenSession
    sess = WakeListenSession(spotter=_StubSpotter(), device="nonexistent0",
                             window_s=0.05)
    t0 = _t.monotonic()
    assert sess.start() is False
    assert _t.monotonic() - t0 < 10.0
    assert sess.state == "error"
    assert sess.error != ""
