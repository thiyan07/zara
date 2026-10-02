# Voice Status (Zara local voice — this document is the truth source)

## Engines (all free, offline, no keys, no accounts)

| Slot | Implementation | Status |
|---|---|---|
| STT | faster-whisper `tiny.en` (int8 CPU) | REAL LINUX AUDIO TESTED |
| TTS | Piper `en_US-lessac-medium` (onnx CPU) | REAL LINUX AUDIO TESTED |
| Wake | VAD-gated single-shot keyword spotter | INTEGRATION TESTED (synth audio) |
| Mic capture | arecord/ALSA (`core/audio.py`) | INTEGRATION TESTED (device works, see below) |
| Speaker | aplay/ALSA | INTEGRATION TESTED (clean exit; audibility not verifiable here) |

## What genuinely works

- Synth speech -> real STT ("Hello Zara, Systems Nominal.", "Check my
  laptop battery.", "Echo back hello.") — REAL LINUX AUDIO TESTED.
- Full local turn: audio -> STT -> conversation loop -> verified tool ->
  NL reply -> TTS WAV -> idle — REAL LINUX AUDIO TESTED (2.0 s).
- Fully offline turn (network calls blocked): STT + echo loop + TTS —
  REAL LINUX AUDIO TESTED. Note: turns needing the hosted NVIDIA LLM
  require network; that path is HOSTED, not local.
- Barge-in cancels chunked TTS; state machine returns to idle.
- Push-to-talk/manual activation is the validated trigger path.

## Blocked / honest limits

- REAL MICROPHONE SPEECH = BLOCKED. The ALC256 capture device records
  successfully but yields digital silence (muted/unconnected input).
  STT itself is validated on synthesized speech, which exercises the
  identical code path.
- REAL WAKE DETECTION = BLOCKED (no mic signal + no DSP). The spotter is
  a VAD-gated single-shot keyword matcher, never an always-on loop, and
  wake events only move voice state — they execute nothing.
- Speaker audibility cannot be verified from here (no ears); playback
  path (valid WAV -> aplay -> exit 0, no zombies) is tested.
- Android: UI/state/permission layer only; no platform STT/TTS wired.
  PHYSICAL ANDROID TESTED: no (unchanged this stage; emulator exists but
  has no functional mic for this purpose).
- Whisper quirks observed: number words normalize ("one two three" ->
  "1 2 3"), punctuation varies run to run. Tests assert accordingly.
