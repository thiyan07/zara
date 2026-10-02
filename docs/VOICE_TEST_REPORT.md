# Voice Test Report (local pipeline)

## Measurements (real, this machine)

- Whisper tiny.en load: 1.4 s (cached) · inference: 0.4 s (2 s audio, CPU)
- Piper synth: ~1 s per short sentence (22050 Hz mono)
- Local STT embed path: n/a (hash/embeddings untouched)
- Full local voice turn (STT -> tool -> TTS): 1.9–2.2 s
- Hosted-LLM turn (for comparison): 38–63 s, one timeout observed
  (loop budget handles it: safe failure message, state preserved)
- Playback: aplay exit 0, 36096 frames
- Disk impact: 63 MB Piper voice + 75 MB whisper tiny.en + ~150 MB pip
  packages (faster-whisper/ctranslate2/piper/webrtcvad; onnxruntime
  pre-existing). Total ~290 MB. Free space 17 GB -> 16 GB (floor 10 GB).
- RAM: models load lazily on first use only; whisper ~300 MB resident
  during inference, released after (no background processes).

## Test levels

- UNIT TESTED: state machine, wake config/phrase, redaction, WAV bounds,
  factories, VAD helper, contract parsing.
- INTEGRATION TESTED: synth->STT, TTS->WAV validity, stream cancel,
  full pipeline turn, offline turn, playback exit, record path, spotter
  on synth "Hey Zara" (detected) vs other speech (rejected).
- REAL LINUX AUDIO TESTED: all of the above ran against real engines
  and real audio files/devices on this laptop.
- BLOCKED: live microphone speech (silence), speaker audibility,
  physical Android audio, always-on DSP wake.

## Security checks in suite

- Transcript injection ("Ignore all policy... rm -rf /") -> hard deny.
- Malicious wake-adjacent text returns data dict only, no execution.
- nvapi-/sk-ant-/sk-proj-style keys now redacted (gap found by this
  stage's own test, fixed in `core/tracing.py`).
- No `shell=True` in audio path; argvs fixed; paths bounded; cancel
  kills process groups (no zombies verified by clean test exits).

## Stage 8 (2026-10-03) — physical conversational voice experience

New: `tests/test_voice_stage8.py` (12 tests): capability-probe structure,
conversation turn-limit + idle-timeout + battery-pause + approval continuity
(same session/mission) + cancel, barge-in monitor (voice True /
silence-capture-failure-cancel False), interrupt-during-speaking,
illegal-transition rejection, transcript-cannot-reconfigure-provider,
Magpie bound/authority checks, wake-output-has-no-authority.

Live measurements (not invented):
- Piper synth "Hey Zara" 33 KB / 0.97 s; WAV 22050 Hz mono valid.
- play_audio via ALSA exit True in 0.01 s (no audible device present) ->
  PLAYBACK_PATH_VERIFIED, AUDIBILITY_MANUAL.
- STT on synth speech: "Hey Zara" -> "Hey Zara!" (1.34 s incl. model load),
  "Check my laptop battery." verbatim (0.34 s), "What is my battery level?"
  verbatim (0.31 s). Wake spotter detects synth "Hey Zara"; VAD rejects silence.
- Magpie live: 96 KB in 2.49 s; cancel mid-stream stops after 1 chunk
  (51 KB), worker thread reaped.
- E2E voice turn (STT->demo-brain->tool->TTS): ok, 30 KB audio, idle, 6.8 s
  (first-turn model load included). Barge-in interrupt: 0 ms, listening.
- Mic probe: opens, permission granted, peak -inf, signal False (ALC256 silent).
- Android emulator API 16: voice-support flags verified, no crash.
- Suite: 142 Python passed; Flutter 10 passed; flutter analyze clean.
- Disk 16 GB free (floor 10 GB respected); no new models downloaded.

Still BLOCKED (honest): live mic speech (silent ALC256), speaker audibility
(no ears on this machine), physical Android (`adb devices` empty), always-on
DSP wake (VAD-gated single-shot retained by design).
