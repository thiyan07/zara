# STT Status (truth source for Zara speech-to-text)

Labels: VERIFIED · PHYSICAL_VERIFIED · TEST_VERIFIED · PENDING ·
BLOCKED_BY_CONFIGURATION · NOT_SUPPORTED · NOT_TESTED · BY_DESIGN.

## Architecture

```
capture (Android mic / WAV) -> STTProvider.transcribe(bytes) -> str
        +-- local faster-whisper tiny.en (default, offline)
        +-- nvidia-parakeet (STT_PROVIDER=nvidia, hosted primary)
        +-- stt-fallback wrapper (nvidia primary + local fallback)
```

`STTProvider.transcribe` still returns `str` — no competing
architecture. Provenance rides on `last_result: STTResult`
(provider/model/audio_ms/wall_ms/fallback_used/fallback_reason) and
flows into `VoicePipeline.handle_audio` output as additive
`stt_provider/stt_model/stt_fallback_used/stt_fallback_reason/stt_wall_ms`
keys. Audit records metadata only — never raw audio, never keys.

## Provider selection (`stt_from_env`)

| `STT_PROVIDER` | Behavior |
|---|---|
| `auto` (default) | local faster-whisper when importable, else mock |
| `local` | require local (fail fast if missing) |
| `nvidia` | hosted Parakeet primary + local fallback |
| `mock` | mock (tests) |

Fallback fires ONLY on transient categories
(RATE_LIMITED/TIMEOUT/NETWORK_ERROR/SERVER_ERROR) and is recorded.
AUTH/CONFIG/INVALID_AUDIO propagate untouched (never retried, never
fallback-masked). One bounded retry (1 s backoff) for transient only.

## NVIDIA hosted STT

- Transport: NVCF gRPC `grpc.nvcf.nvidia.com:443`, function-id +
  Bearer key (mirrors Magpie TTS). Key: `NVIDIA_STT_API_KEY` else
  `LLM_API_KEY`, backend Core only. NEVER in APK/logs/audit/errors.
- Default model: `parakeet-tdt-0_6b-v2` (English).
  Function id `d3fe9151-442b-4204-a70d-5fcc597fd610` (rotatable via env).
- REAL request VERIFIED 2026-10-04 (Piper-synthesized
  "Hello, I am Zara. What is my battery level?" ->
  "hello i am zara what is my battery level").
- Pricing/quota: NOT verified — no free/unlimited claim is made.
  Hosted use is metered by the NVIDIA account; offline fallback exists
  for exactly this reason.

## Audio contract

WAV/PCM mono (server accepts rate as captured; 16 kHz canonical).
Bounds enforced BEFORE transmission: max 30 s, max 2 MB
(`MAX_STT_SECONDS`/`MAX_STT_BYTES`, mirroring capture + voice-turn caps).
Empty/invalid/oversize audio rejected with INVALID_AUDIO, no network.

## Failure categories (deterministic)

AUTH_ERROR · RATE_LIMITED · TIMEOUT · NETWORK_ERROR · SERVER_ERROR ·
INVALID_AUDIO · INVALID_RESPONSE · EMPTY_TRANSCRIPT ·
CONFIGURATION_ERROR · CANCELLED. Mapped from gRPC status; errors carry
status names only. Cancellation: `stop()` drops the channel; the
pipeline discards the result; `interrupt()` now stops STT too.

## Governor

ONLINE: NVIDIA preferred. DEGRADED: NVIDIA if healthy else local.
OFFLINE: local only (hosted fails NETWORK_ERROR -> fallback).
CRITICAL: existing refusal behavior (no STT work scheduled).
Wake loop: unchanged — no always-on mic; "Hey Zara" via manual capture.

## Benchmark (2026-10-04, `scripts/stt_bench.py`, project-owned Piper fixtures)

| provider | mean latency | mean WER* | mean token recall | exact |
|---|---|---|---|---|
| local tiny.en | 0.37 s | 0.266 | 0.705 | 6/13 |
| nvidia parakeet | 0.62 s | 0.181 | 0.750 | 7/13 |

13 fixtures: battery/tech/numbers/filename/short + longcmd/pytest/nim/
deletetemp/threshold/opencode/fast(1.4x time-compressed)/noisy(10dB SNR).
*WER on lowercased alnum normalization (punishes "FastAPI"->"fast api";
read qualitatively). Neither dominates: NVIDIA wins numbers/postpone +
overall WER; local is faster and matched NVIDIA on 10/13. Both fail
`pytest tests/test_voice.py` spelling and 10dB noise ("factory level").
Fast/noise fixtures are labeled stress (compressed/synthetic), not
human speech. Accent/Tamil: NOT_TESTED here — covered physically instead.

| provider | mean latency | mean WER* | exact (5-fixture pilot) |
|---|---|---|---|
| local tiny.en | 0.52 s | 0.067 | 4/5 |
| nvidia parakeet | 0.71 s | 0.124 | 3/5 |

*Pilot (Stage 12) WER on lowercased alnum normalization (punishes
"FastAPI"->"fast api"; read qualitatively). The 13-fixture table above
supersedes it. Notable: NVIDIA got "postpone" right where
tiny.en heard "Post-pung" — the motivating quality gap.

## Language status

- English: VERIFIED (both providers, live + fixtures).
- Indian English: PHYSICAL_VERIFIED 2026-10-04 (vivo V2338 mic ->
  Core STT_PROVIDER=nvidia -> `stt: nvidia-parakeet parakeet-tdt-0_6b-v2`,
  no fallback; "hey zara run the tests" exact at peak -26.9 dBFS;
  fast "FastAPI" misheard as "1st apa"/"fast apst" — tech-term gap
  recorded, not hidden).
- Technical vocabulary: TEST_VERIFIED (FastAPI/PostgreSQL/OpenCode terms
  in fixtures; casing normalization caveat above).
- Tamil: NOT_SUPPORTED by selected model (parakeet-tdt English-only).
  whisper-large-v3 function (ta-IN) answers requests (reachability
  VERIFIED 2026-10-04) but no Tamil audio exists to test transcription
  with — no claim made. INVESTIGATION_ONLY, never auto-selected.
- Tanglish: NOT_TESTED (no fixture voice; no private recordings used).
- Fast speech / noise: NOT_TESTED in harness; noise covered physically.

## Security (all TEST_VERIFIED)

Key in no logs/exceptions/transcript/audit/response (tests assert).
APK contains no secret (no Dart change; Core-only key). Transcript =
untrusted data (existing hard-deny 403 intact, incl. new regression
test). Empty/cancelled/timeout transcripts execute nothing (pipeline
returns error state; verified by existing voice tests).

## Stage 13 additions (2026-10-04)

- Transcript safety signals (`core/voice_safety.py`): deterministic
  empty/too-short/high-risk/missing-target/number/urgency flags,
  attached to turn output as `transcript_flags` (advisory only;
  enforcement stays in policy + approvals). TEST_VERIFIED.
- Provider health (`STTHealth`): consecutive failures, last category,
  60 s cooldown after 3 transient failures (fallback reason=cooldown),
  rolling 20-sample mean latency. Memory-bounded, no audio persisted.
- Latency breakdown: turn output now carries `loop_ms` alongside
  `stt_wall_ms` (capture/upload/LLM/tool/TTS split: capture 5 s bounded,
  STT 0.5–3.3 s, loop varies with LLM; live full turn 3.3 s NVIDIA /
  8.5 s fallback path incl. retry).
- Offline fallback validated LIVE (black-holed NVIDIA server ->
  NETWORK_ERROR -> local "Run the tests.", fallback=True, responded).
  True radio-off on hardware BLOCKED (laptop rides phone hotspot).
- Barge-in: software cancellation TEST_VERIFIED (incl. STT stop +
  failing-stop races); acoustic PENDING.
- Cold-start register-retry deadlock fixed in app (separate fix, same
  branch): PHYSICAL_VERIFIED (dead-tunnel boot -> auto-online).
