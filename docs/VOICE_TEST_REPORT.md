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
