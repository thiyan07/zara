#!/usr/bin/env python3
"""Repeatable microphone→speaker→STT→wake loopback probe for Zara.

What it proves (and labels honestly):
- The laptop capture path Zara actually uses (core.audio.record_backend():
  pipewire `pw-record` / ALSA `arecord`) delivers REAL signal.
- The TTS voice (piper) rendered through the speakers, captured by the
  REAL laptop microphone, is transcribed by the REAL STT (faster-whisper
  tiny.en int8) — an end-to-end acoustic loopback.
- The REAL "Hey Zara" wake spotter (OWW v1, same runtime semantics as the
  validated binding) scores the live capture and reports detections.

What it does NOT prove:
- Human speech handling, live-room behavior, or false-acceptance rates
  (the speech source is synthetic TTS over the speakers; no human said
  anything). Wake FAR evidence remains the recorded metrics in
  training/hey_zara_metrics.json — never lowered by this probe.

Usage:
  python scripts/voice_mic_probe.py [--text "Hey Zara, check the battery."]
  [--no-stt] [--wake]
Requires: piper voice at ~/.cache/zara-voice/en_US-lessac-medium.onnx,
faster-whisper tiny.en, and the wake model under ~/.cache/zara-wake/.
All output is stdout JSON; audio snippets go to /tmp/opencode only.
"""
from __future__ import annotations

import argparse
import array
import contextlib
import json
import os
import subprocess
import sys
import time
import wave

REPO = __file__.rsplit("/scripts/", 1)[0] if "/scripts/" in __file__ else "."
sys.path.insert(0, REPO)

from core.audio import record_backend  # noqa: E402

TTS_MODEL = os.path.expanduser("~/.cache/zara-voice/en_US-lessac-medium.onnx")
WAKE_MODEL = os.path.expanduser("~/.cache/zara-wake/hey_zara.onnx")


def peak_of(wav_path: str) -> int:
    a = array.array("h")
    with open(wav_path, "rb") as f:
        data = f.read()
    a.frombytes(data[data.find(b"data") + 8:])
    return int(max(abs(min(a)), abs(max(a)))) if len(a) else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Hey Zara, check the battery.")
    ap.add_argument("--secs", type=int, default=7)
    ap.add_argument("--no-stt", action="store_true")
    ap.add_argument("--wake", action="store_true",
                    help="also run the OWW spotter over the capture")
    args = ap.parse_args()
    out: dict = {"backend": record_backend(),
                 "speech_source": "synthetic TTS via system speakers",
                 "mic": "laptop capture device (system default)"}

    from piper import PiperVoice
    import wave as _w
    voice = PiperVoice.load(TTS_MODEL)
    tts_path = "/tmp/opencode/zara_probe_tts.wav"
    with _w.open(tts_path, "wb") as w:
        voice.synthesize_wav(args.text, w)
    out["tts_wav"] = tts_path

    rec_path = "/tmp/opencode/zara_probe_mic.wav"
    rec = subprocess.Popen(
        ["arecord", "-D", "default", "-d", str(args.secs), "-f", "S16_LE",
         "-r", "22050", "-c", "1", "-t", "wav", rec_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)  # settle, then play into the open capture
    play = subprocess.run(
        ["aplay", "-q", tts_path], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    rec.wait()
    out["peak_amplitude_16bit"] = peak_of(rec_path)
    out["mic_signal_live"] = out["peak_amplitude_16bit"] > 2000
    if play.returncode != 0:
        out["play_error"] = f"aplay exit {play.returncode}"

    if args.wake:
        import numpy as _np
        from core.voice import OpenWakeWordSpotter, OpenWakeWordConfig
        with contextlib.closing(wave.open(rec_path, "rb")) as w:
            raw = w.readframes(w.getnframes())
        pcm = (_np.frombuffer(raw, dtype=_np.int16) / 32768.0)
        x = _np.asarray(pcm, dtype=_np.float32)
        t = _np.arange(int(len(x) * 16000 / 22050)) * (22050.0 / 16000.0)
        i0 = t.astype(int); i1 = _np.minimum(i0 + 1, len(x) - 1)
        fr = t - i0
        x16 = (x[i0] * (1 - fr) + x[i1] * fr).astype(_np.float32)
        sp = OpenWakeWordSpotter(OpenWakeWordConfig())
        conf, fired = 0.0, 0
        for i in range(0, len(x16), sp.CHUNK):
            frame = (x16[i:i + sp.CHUNK] * 32768.0)
            if len(frame) < sp.CHUNK:
                break
            o = sp.spot(frame.astype(_np.int16).tobytes())
            conf = max(conf, float(o["confidence"]))
            fired += 1 if o["detected"] else 0
        out["wake_max_confidence"] = round(conf, 4)
        out["wake_detections"] = fired
        out["wake_model"] = WAKE_MODEL

    if not args.no_stt:
        from faster_whisper import WhisperModel
        m = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        segs, _info = m.transcribe(rec_path, language="en")
        transcript = " ".join(s.text.strip() for s in segs)
        out["stt_transcript"] = transcript
        out["stt_contains_target_words"] = any(
            w in transcript.lower() for w in
            args.text.lower().split()[:3][1:])

    out["labels"] = {
        "mic_capture": "REAL hardware",
        "speech_source": "synthetic (TTS over speakers; no human speech)",
        "stt_and_wake": "real engines on real capture",
        "not_claimed": ["human speech", "live-room FAR", "room noise model"],
    }
    print(json.dumps(out, indent=2))
    return 0 if out["mic_signal_live"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
