# Voice Status (Zara local voice — this document is the truth source)

## Engines (all free, offline, no keys, no accounts)

| Slot | Implementation | Status |
|---|---|---|
| STT | faster-whisper `tiny.en` (int8 CPU) | REAL LINUX AUDIO TESTED |
| TTS | Piper `en_US-lessac-medium` (onnx CPU) | REAL LINUX AUDIO TESTED |
| Wake | VAD-gated single-shot keyword spotter | INTEGRATION TESTED (synth audio) |
| Mic capture | arecord/ALSA (`core/audio.py`) + `probe_capabilities()` | VERIFIED opens, BLOCKED signal (see below) |
| Speaker | aplay/ALSA | PLAYBACK_PATH_VERIFIED, AUDIBILITY_MANUAL |
| Conversation | `VoiceConversation` bounded sessions (`core/voice.py`) | INTEGRATION TESTED |
| Barge-in monitor | `monitor_barge_in` VAD polling (`core/voice.py`) | INTEGRATION TESTED |
| Android audio flags | mic-hw/permission/signal + speaker-hw/engine presence (`DeviceBridge`) | EMULATOR VERIFIED |

## What genuinely works

- Synth speech -> real STT ("Hello Zara, Systems Nominal.", "Check my
  laptop battery.", "Echo back hello.") — REAL LINUX AUDIO TESTED.
- Full local turn: audio -> STT -> conversation loop -> verified tool ->
  NL reply -> TTS WAV -> idle — REAL LINUX AUDIO TESTED (2.0 s).
- Fully offline turn (network calls blocked): STT + echo loop + TTS —
  REAL LINUX AUDIO TESTED. Note: turns needing the hosted NVIDIA LLM
  require network; that path is HOSTED, not local.
- Barge-in cancels chunked TTS; state machine returns to idle.
- Continuous conversation: bounded sessions (max turns/duration/idle
  timeout, battery hook, approval-hold resume in the same session) —
  INTEGRATION TESTED (12 Stage-8 tests).
- Barge-in monitor: bounded VAD polling (hard timeout + cancel); voiced
  chunk -> True, silence/capture-failure/cancel -> False — INTEGRATION TESTED.
- Audio capability probe: device opens, permission granted, peak -inf
  (digital silence), signal=False — VERIFIED 2026-10-03. Playback exits 0
  via ALSA in 0.01 s (no audible device here) — PLAYBACK_PATH_VERIFIED,
  AUDIBILITY_MANUAL.
- Piper STT roundtrip on synth speech (2026-10-03): "Hey Zara" ->
  "Hey Zara!", "Check my laptop battery." verbatim, "What is my battery
  level?" verbatim; wake spotter detects synth "Hey Zara", rejects silence.
- Magpie live (2026-10-03): 96 KB in 2.49 s; mid-stream cancel stops
  after 1 chunk, thread reaped. Piper remains default (`TTS_PROVIDER`
  unset -> PiperTTS); transcript cannot reconfigure providers (tested).
- Android emulator (API 16, 2026-10-03): `getVoiceSupport` reports
  mic-hardware true, permission false, signal unknown, speaker-hardware
  true, platform STT/TTS engines true, Zara capture/STT/TTS/wake false
  (honest: unimplemented == false). No crash. EMULATOR VERIFIED.
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
- Android: full body runtime (Stage 9 — see `docs/ANDROID_STATUS.md`):
  lifecycle/restore/revoke, allowlisted job execution, voice session with
  Core-identical limits, notification approvals via Core, mock push + poll
  fallback. EMULATOR VERIFIED 2026-10-03 (pairing, jobs, revoke, audio API
  paths). PHYSICAL ANDROID: PENDING (mic signal, audibility, DSP wake).
- Whisper quirks observed: number words normalize ("one two three" ->
  "1 2 3"), punctuation varies run to run. Tests assert accordingly.

## NVIDIA Magpie (optional hosted)

REAL API TESTED 2026-10-02: "Hello, I am Zara." -> 1.2 s -> valid 22050 Hz
WAV (39k frames), STT-verified exact, clean stop. Same Zara turn run with
Piper (1.0 s, 62 KB) and Magpie (1.2 s, 78 KB): identical pipeline, both
responded. Network-dependent by design; offline fails truthfully.
`integrate.api.nvidia.com/v1` hosts NO TTS model (81 models listed, all
LLM/vision/embed) — Magpie is reached via NVCF gRPC, not the LLM endpoint.
