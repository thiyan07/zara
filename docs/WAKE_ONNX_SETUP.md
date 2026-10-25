# Wake-word setup: ONNX detector + "Hey Zara" model path

Status: **detection live on this laptop (binding validated, model
trained on the user's own recordings)** — `OpenWakeWordSpotter`
(`core/voice.py`) runs the trained "Hey Zara" model fully offline over
onnxruntime, and is differentially validated against the upstream
openWakeWord reference implementation (331 streaming calls, identical
scores). Measured behaviour lives in `training/hey_zara_metrics.json`
(recall 0.954 @ threshold 0.5 over 1009 positive clips — 1000 synthetic
+ 9 real user takes that all fire ≥0.99; adversarial soundalike
negatives give an elevated false-accept rate that no threshold meets a
0.5 FP/hr bar — see that file's `limitation`). Nothing is downloaded
automatically; a machine without the model files fails closed with an
actionable error.

## Engine selected and why

**onnx-wakeword runtime design** (https://github.com/voicute/onnx-wakeword):
offline causal-TCN keyword spotting, ~130 KB models, <5–10 ms/frame on
desktop CPU, consecutive-frame + cooldown + energy anti-false-trigger
layers, Python + Android + Linux coverage, no cloud, no audio upload.
It fits Zara's budget (1-thread sessions, 16 kHz bounded snippets, no
STT/LLM/network before detection).

Two deliberate non-reuses:

- **No vendored engine code.** The upstream repository ships no license
  file despite an "Open Source" claim, so its `wakeword_engine.py` /
  `WakeWordEngine.java` are NOT copied into Zara. `OnnxWakeSpotter`
  (`core/voice.py`) is Zara's own thin adapter over the already-installed
  `onnxruntime` package, implementing the documented model contract
  (mel frontend + keyword head + `model_info.json` keywords).
- **No model shipped.** Keyword models are custom-trained per phrase;
  a generic English model never counts as "Hey Zara" (the spotter
  refuses when the configured keyword is absent from `model_info.json`).

## The openWakeWord route (implemented, validated binding)

`OnnxWakeSpotter` above is engine B. Engine A — the one actually in use
on this laptop — is `OpenWakeWordSpotter` (same `core/voice.py`), the
openWakeWord (Apache-2.0) custom-model path:

- Model: `~/.cache/zara-wake/hey_zara.onnx` — trained from the
  workspace `~/.cache/zara-wake-train/` (piper-sample-generator
  positives + adversarial soundalike negatives + FMA/RIR augmentation;
  user's own 9 recorded takes held out and used for validation). The
  openWakeWord release frontends (`melspectrogram.onnx`,
  `embedding_model.onnx`, ~2.4 MB) live in `~/.cache/zara-wake/oww/`.
- Runtime: numpy + onnxruntime only — the streaming semantics
  (80 ms chunks; mel → 76-frame embedding windows → 16-frame classifier
  window; per-call max over new chunks; <1280-sample calls repeat the
  previous score; first five scored calls warm up at 0.0) are
  re-implemented exactly and differentially validated against the
  upstream reference (`sys.path` import of the training checkout when
  present): **331 calls over real clips, 0 mismatches (≤5e-6)**.
- Policy on top of raw scores: threshold 0.5 (env
  `WAKE_OWW_THRESHOLD`) → `cons_frames` 2 consecutive confirmed chunks
  → 1.5 s cooldown. `ZARA_WAKE_ENGINE` selects the engine


  (`auto` | `oww` | `onnx`; auto prefers the trained openWakeWord model
  when all three files exist; unknown values are refused loudly).
- `wake_listen_once` and `WakeListenSession` default to
  `ZARA_WAKE_ENGINE` auto-selection (explicit spotter arguments always
  win; tests inject their own). The default spotter is a process-wide
  persistent engine — a fresh engine per call could never pass the
  reference warm-up.
- Known measured limitation: "hey Sarah"-class soundalikes in the
  adversarial negative set trigger a false accept at meaningful rates
  (training/hey_zara_metrics.json). A wake event only opens a voice
  turn; the turn is then transcribed by STT and must match the wake
  phrase / intent flow — a false wake therefore costs a short
  listening window, never an action. Improving the negative corpus
  (more synthetic soundalike + real-room ambient hours) is the known
  training-time remedy and is disk-budget-limited on this laptop.

## Inference contract (implemented in `OnnxWakeSpotter`)

Matches the validated upstream engine (read, not vendored — no license
file ships upstream):

- 16 kHz mono s16 in; int16 → float32; padded/clipped to exactly
  `mel_time * 160 + 400` samples (≈1.005 s at `mel_time=98`).
- Mel frontend input `(1, N)` → output `(1, 1, frames, 32)`; normalized
  `/10 + 2`; last `mel_time` frames into `(1, mel_time, n_mels)` with
  `n_mels` resolved from the keyword model's own input shape.
- Keyword output `[1, N]` raw scores → sigmoid + background-class
  softmax → best word + confidence → threshold gate (default `0.4`,
  upstream default) → consecutive-frames confirmation (`cons_frames`
  from `model_info.json`, default 2) → cooldown (default 1.5 s).
- Sessions: single-threaded (`intra_op_num_threads=1`), CPU provider
  only, log level errors-only, created once and reused.
- `model_info.json` or a `.zip` bundle containing it (ZIP entries read
  directly, never extracted). The configured keyword must be listed —
  a generic model never counts as "Hey Zara".

## Linux capture (`capture_snippet_alsa` + `wake_listen_once`)

- One bounded `arecord` snippet (1–5 s, 16 kHz enforced, single
  foreground subprocess with timeout, raw PCM, nothing persisted).
- `wake_listen_once` validates the model BEFORE touching the mic, so
  a missing model never costs a room recording. No loop, no background
  thread — continuous listening stays an explicit, ungated product
  decision, not something this helper does.
- Missing binary / bad params / ALSA errors / timeouts / empty
  captures all raise honestly (unit-tested with stubs; no live mic
  test performed — see limits).

## Obtaining a "Hey Zara" model (manual, authorized steps only)

1. Train the phrase at the vendor console (their service, ~30 min;
   false-trigger optimization pass recommended). No data submitted in
   this run — ask the user before submitting anything, and never
   upload private recordings (vendor training uses TTS-generated data
   by default).
2. Download the resulting `model.zip`: `model_info.json` (must list
   `"Hey Zara"`), the keyword `.onnx`, and `melspectrogram.onnx`.
3. Place them under `~/.cache/zara-voice/wake/` (or set the env vars):
   - `WAKE_ONNX_MODEL_INFO` (default `~/.cache/zara-voice/wake/model_info.json`)
   - `WAKE_ONNX_KEYWORD` (default `.../wake/hey_zara.onnx`)
   - `WAKE_ONNX_MEL` (default `.../wake/melspectrogram.onnx`)
   - `WAKE_ONNX_KEYWORD_NAME` (default `Hey Zara`)
   - `WAKE_ONNX_THRESHOLD` (default `0.4`, upstream default),
     `WAKE_ONNX_COOLDOWN_S` (default `1.5`), `WAKE_ONNX_MAX_SNIPPET_S`
     (default `5.0`)
4. Verify locally: `OnnxWakeSpotter().availability()` must report
   `ready: true`; spot a bounded 16 kHz mono s16 snippet and confirm a
   `detected` result before claiming anything.
5. Android needs the same model packaged with a native AudioRecord loop
   plus a foreground-service design — NOT implemented (see below).

Do NOT download demo models for other keywords and present them as
"Hey Zara" support. Do NOT train from scratch in this repo.

## Switching detection ON and OFF (Android app)

Settings → **Wake-word opt-in ("Hey Zara")**:

- The switch is the persisted preference (plain marker file, default
  OFF on first install). Flipping ON attempts exactly one detector
  start after mic permission, model availability, and battery gating
  re-validate; repeated taps never duplicate listeners.
- Visible states: `detection: off | starting… | listening (foreground
  only) | error — <actionable cause>`.
- Enabling may increase battery usage (note shown under the toggle).
- **Stop listening now** disables immediately without leaving Settings.
- Preference restores across restarts but listening never auto-starts
  (re-enable explicitly in the foreground). Preference never overrides
  a revoked permission, missing model, or failed init.
- Pause, logout, revocation, and disposal all park the listener
  (late wake events are dropped by generation + cooldown).

## Platform limits (current, honest)

- **Android:** foreground-only. No background service, WorkManager job,
  or battery exemption is added — continuous background listening is
  unavailable under current lifecycle rules, and the UI says so.
  If background listening is ever justified, the compliant route is:
  `FOREGROUND_SERVICE_MICROPHONE` permission + `microphone`
  foreground-service type + a persistent user-visible notification +
  explicit opt-in + AudioRecord bounded buffers feeding the same
  `(1,mel_time,n_mels)` contract, with the existing toggle/generation
  guards unchanged. Battery cost must be measured before claiming
  anything. None of this is implemented; foreground-only stays the
  safe fallback.
- **Linux:** `OnnxWakeSpotter` scores bounded snippets; `arecord`
  streaming sessions (`WakeListenSession`) need a model to detect.
  Live mic runs need a private setting + explicit consent each time.
- **Privacy:** inference local only; no audio persisted; no mic
  contents in logs/diagnostics; a wake event never authorizes anything
  by itself (normal approval flow still applies).
- **OCI:** wake processing never moves to the cloud; post-wake traffic
  is the normal bounded text/task API.

## What remains blocked

1. Live-room validation: continuous `WakeListenSession` runs against
   the real room (false-accept/reject rates on ambient + real noise,
   latency, idle CPU/RAM, battery OFF-vs-ON) need the user's explicit
   consent to run the mic in-session — acoustic evidence so far comes
   from the user's own recorded takes and the synthetic sets.
2. Persistent wake reliability: the adversarial soundalike false-accept
   class noted above (more/better negative data at training time; the
   ACAV100M background feature file (~15 GB) stayed out of disk budget).
3. Android native audio loop + model packaging (needs 1 + a foreground
   service design; see Platform limits above).
