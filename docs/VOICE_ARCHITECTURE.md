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

## Honest limits (Stage 3)

No physical microphone/speaker validation performed (no hardware harness);
pipeline verified with REAL local engines end-to-end (synth speech -> STT ->
core -> tool -> device -> TTS) plus deterministic mocks (STT->core->tool->
device->TTS). Android reports real support flags (`getVoiceSupport`);
capture/STT/TTS/wake engines report `false` until provider-backed builds land
with hardware validation.

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
