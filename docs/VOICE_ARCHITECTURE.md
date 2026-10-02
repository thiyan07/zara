# Voice Architecture (`core/voice.py`, Android `lib/voice.dart`)

```
mic -> audio capture -> STT -> Zara core loop -> LLM -> tool/device ->
verified result -> NL reply -> TTS -> speaker
```

Voice is an interface, never an authority: the phone never commands devices
directly; everything flows through policy/governor/router/verification.

## Abstractions (all replaceable, mock-backed in tests)

- `STTProvider.transcribe / stream_transcribe` (partials where supported,
  whole-audio fallback otherwise — never faked streaming).
  `FasterWhisperSTT` (DEFAULT) runs tiny.en int8 on CPU, offline; input is
  WAV bytes converted to 16 kHz mono in-process (numpy, already a dep).
- `TTSProvider.speak / stream_speak(cancel)` — chunked synthesis so barge-in
  cancels per chunk. `PiperTTS` (DEFAULT) synthesizes locally via onnx CPU,
  sentence-chunked; `MockTTS` records; `EdgeTTSProvider` remains available
  only when explicitly configured (`TTS_PROVIDER=edge`) — never the default.
- `VoiceStateMachine`: idle->listening->transcribing->thinking->executing->
  speaking->idle, with interrupted/error branches. Illegal jumps raise; one
  owner, no scattered flags. `interrupt()` = barge-in: cancel TTS, back to
  listening, same session (no fork).
- `VoicePipeline.handle_audio`: STT failure -> error state, no hallucinated
  transcript; tool result -> spoken summary; empty audio rejected.

## Battery

Voice work is gated like everything else: STT/TTS are not free — short
utterances, no background loops, heavy reasoning stays on laptop/cloud via
the router. Wake listening pauses on low battery (see WAKE_WORD.md).

## Honest limits (Stage 3; Stage-8 deltas noted inline)

No physical microphone/speaker validation performed (no hardware harness);
pipeline verified with REAL local engines end-to-end (synth speech -> STT ->
core -> tool -> device -> TTS) plus deterministic mocks (STT->core->tool->
device->TTS). Android reports real support flags (`getVoiceSupport`);
capture/STT/TTS/wake engines report `false` until provider-backed builds land
with hardware validation. Stage 8: emulator flags verified live (mic-hw
true, speaker-hw true, engines present, Zara engines honestly false);
Linux probe verified device-opens-but-silent; speaker path exits 0 with
audibility manual.

## Capability probe (`probe_capabilities`, Stage 8)

Deterministic report separating exists / permitted / opens / carries-signal
/ plays / completes. Signal threshold: peak above -50 dBFS; silence is
reported as `microphone_signal_detected: false` with `peak_dbfs: null`,
never hidden. Exit-0 playback is reported as path-verified, never as
human-heard.

## Continuous conversation (`VoiceConversation`, Stage 8)

Bounded multi-turn sessions over ONE `VoicePipeline`: max turns (default 5),
max wall-clock duration (300 s), idle timeout (60 s), battery hook, cancel.
Approval holds pause and resume in the SAME session (mission/approval state
preserved; resumed reply re-enters the legal state path). Per-turn loop
budgets intact; no autonomous loop. The LLM never touches mic, TTS provider,
permissions, or devices.

## Barge-in monitor (`monitor_barge_in`, Stage 8)

Bounded VAD polling over mic chunks: first voiced chunk -> True (caller runs
`VoicePipeline.interrupt()`), with hard timeout + cancel event so a silent
mic returns False instead of hanging. Provider cancel respected (Magpie
in-flight RPC cancel preserved; Piper chunked stop).

## Local audio layer (`core/audio.py`)

Bounded `record_audio()` / `play_audio()` over system utilities (ALSA
preferred — verified clean WAV; PipeWire fallback). No background loops;
recording only happens on state-machine-authorized turns; cancellation
kills the process group. WAV validated with stdlib `wave`.

## Wake backend (`VADSpotterBackend`)

VAD-gated single-shot keyword spotter on bounded snippets (<= 5 s): trims
silence via WebRTC VAD, transcribes locally, matches exactly "Hey Zara".
It is NOT always-on DSP and never executes — detection only moves voice
state, subject to the existing battery gating. Push-to-talk/manual
activation is the validated trigger path. See VOICE_STATUS.md for the
honest capability table.

## NVIDIA Magpie (optional hosted TTS, `core/tts_nvidia.py`)

Same `TTSProvider` interface; selected ONLY via `TTS_PROVIDER=nvidia|magpie`.
Contract verified live: gRPC `grpc.nvcf.nvidia.com:443`, function-id +
Bearer metadata, model `magpie_tts_ensemble-Magpie-Multilingual`, voice
`Magpie-Multilingual.EN-US.Aria`, LINEAR_PCM 22050 Hz mono. Per-sentence
requests (<=200 chars) match Piper's barge-in granularity; cancel aborts
the in-flight RPC without tearing down the client. Missing key or any
failure raises `MagpieError` (never the key) — no silent Piper fallback.
Reuses `LLM_API_KEY` unless `NVIDIA_TTS_API_KEY` is set. Piper remains the
default and works with zero network. Free-endpoint status may change;
nothing depends on Magpie.
