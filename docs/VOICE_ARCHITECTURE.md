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
- `TTSProvider.speak / stream_speak(cancel)` — chunked synthesis so barge-in
  cancels per chunk. `MockTTS` records; `EdgeTTSProvider` synthesizes via the
  free keyless edge-tts package (network needed; lazy import; configured only).
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
pipeline verified with deterministic mocks end-to-end (STT->core->tool->
device->TTS). Android reports real support flags (`getVoiceSupport`);
capture/STT/TTS/wake engines report `false` until provider-backed builds land
with hardware validation.
