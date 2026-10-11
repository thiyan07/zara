# CALIBRATION CAMPAIGN — measured evidence (Task 8)

**Status: MEASUREMENT ONLY. No thresholds derived. No weights. Governor v2
remains NOT READY until the derivation memo + remaining gates complete.**

## Plan and methodology

Cells = platform × workload: linux{idle,light,moderate} + android
{idle,light} (android-moderate unavailable: no safe stress path on the
emulator). Target: 30 independent spaced samples per cell (150 total).

- Linux: `scripts/calibration_campaign.py` — serial phases; per-sample
  workload window (~20 s) + spacing (~25 s); distinct UTC timestamps;
  stdlib only; ceilings (60 s wall, 100 MB RSS, 70% 1-core); cleanup
  per window (buffers released, scratch removed).
- Android: passive reads of Core device view (app heartbeats every
  30 s); idle = foreground app untouched; light = throttled monkey
  (≤2 ev/s, ≤200 events, fixed seed) in background of sampling.
- Intended vs measured: every record labels intended workload class AND
  measured values; class never inferred from command alone.
- No sleeps-as-sync, no drains, no dispatch (all records
  `execution.state=not_executed`).

## Workloads and safety ceilings

| Cell | Workload | Duration | Ceiling | Cleanup |
|---|---|---|---|---|
| linux-idle | sleep | 20 s/sample | ~0% CPU | exit |
| linux-light | 30 ms busy / 70 ms sleep, nice 10 | 20 s/sample | ≤35% 1 core | exit |
| linux-moderate | 150 ms busy / 100 ms sleep + 50 MB hold | 20 s/sample | ≤70% 1 core, ≤100 MB | buffer released |
| android-idle | foreground, no input | observe 40 s/sample | 0 ev/s | back to home |
| android-light | monkey throttle 500, 200 ev, seed fixed | ~4 min/run | 2 ev/s, syskeys 0 | force-stop pkg |

Global stops: wall exceeded, ceiling breach, thermal >75 °C, load
>0.7×ncpu, ANR/OOM/crash, disk ≤10 GB. None triggered (see §errors).

## Sample counts (collected 2026-10-10, Night 7)

| Cell | Valid | Invalid | Duplicates | Suitable |
|---|---|---|---|---|
| linux-idle | 30 | 0 | 0 | 30 |
| linux-light | 30 | 0 | 0 | 30 |
| linux-moderate | 30 | 0 | 0 | 30 |
| android-idle | 30 | 0 | 0 | 30 |
| android-light | 30 | 0 (+12 stale/duplicate windows skipped, uncounted) | 0 | 30 |
| android-moderate | UNAVAILABLE (no safe path) | — | — | — |

Total: 150 measured / 0 invalid / 0 duplicate / 150 suitable (90 linux
+ 60 android). Collected 2026-10-10 (Night 8): linux via
`scripts/calibration_campaign.py`, android via
`scripts/calibration_android.py` against Core :18097 + ZaraLab AVD
(API 36, debug build, paired+trusted, foregrounded for idle; monkey-
on-app light per §errors).

## Statistics (measured, descriptive only — no thresholds derived)

| Cell | cpu_pct (min/max/mean/med, n=30) | mem_pct (min/max/mean/med, n=30) |
|---|---|---|
| linux-idle | 1.7 / 68.1 / 10.8 / 4.2 | 47.0 / 85.0 / 74.4 / 76.1 |
| linux-light | 1.7 / 85.8 / 8.6 / 4.2 | 70.6 / 82.5 / 78.7 / 81.2 |
| linux-moderate | 1.7 / 27.6 / 9.7 / 4.2 | 82.1 / 83.4 / 82.9 / 82.8 |

| android-idle | mem 55.0/57.7/56.6/56.6 (n=30), thermal always none |
| android-light | mem 54.8/58.0/55.9/55.6 (n=30), thermal always none |
| android-moderate | n/a |

Notes: host background load (incl. concurrent emulator boot) widens
idle/light CPU spread — recorded as observed, not filtered. mem_pct
reflects whole-host pressure, not the workload buffers alone. No
threshold crossings claimed; no weights derived.

## Provenance and reproducibility

- Collector: `python3 scripts/calibration_campaign.py --out DIR --phase
  {all|idle|light|moderate} --n 30` (Linux); Android via Core
  `/v1/fabric/devices/emu-lab` reads scripted with sleeps (commands in
  session log; cadence ~40 s).
- Dataset: `docs/research/evidence/calibration_v2.jsonl` + `.summary.json`
  (NEW files; v1 untouched, hash-verified).
- Hashes (2026-10-10): jsonl sha256
  b3c699ce506befb68dfeb6c4265979fe91cb7b84ec550e4677e4d7a9dd5a907b;
  summary sha256
  3dc898e9edfeee627deecac943c776d4b7700ce8059e2b69109915309b581e81
  (verify with `sha256sum docs/research/evidence/calibration_v2.*`).
- Validation: `tests/test_calibration_campaign.py` (schema, spacing,
  bounds, secrets, counts, v1 stability).

## Errors and aborts (Nights 7–8)

- Night 7's Android blockage RESOLVED (Night 8): the nulls were a
  revoked-key pairing stall (my own enroll/revoke loop killed the key;
  error-state app never ticks), not a pipeline defect. Re-paired cleanly;
  heartbeats carry full telemetry. Lesson recorded: presence/trust
  readings predate the stall — verify `last_telemetry_at`, not presence.
- Android idle/light collection BLOCKED at the telemetry layer (not the
  pairing layer): emulator booted (ZaraLab AVD, API 36), debug APK built

- Android idle/light collection BLOCKED at the telemetry layer (not the
  pairing layer): emulator booted (ZaraLab AVD, API 36), debug APK built
  with `ZARA_CORE_URL=http://10.0.2.2:18097`, installed, enrolled +
  claimed + registered via screenshot-guided taps (heartbeat presence
  `online`, trust `trusted`). But every heartbeat carries zero telemetry
  fields (`mem_pct`/`storage_*`/`battery_temp_c`/`thermal_state` all null,
  `last_telemetry_at` never set). Native `deviceTelem()` looks correct;
  the Dart tick or bridge returns empty on this build — needs app-side
  debugging beyond this shift's budget. No Android records fabricated;
  cells recorded as 0 with this reason.
- `adb shell input text` is unreliable for mixed-case codes on this
  emulator (drops/mangles after ~2 chars under load); worked around with
  fresh-field + slow 5-char chunks + hierarchy verification. Pairing TTL
  is 600 s — one code expired mid-campaign (re-enrolled after revoke).
- App backgrounding pauses the tick loop: monkey-on-Settings stalled
  heartbeats (frozen `last_telemetry_at`); monkey-on-Zara with Sign-out
  scrolled off-screen keeps ticks flowing. Reader skips stale windows
  (12 skips) and duplicate measurement instants (same `last_telemetry_at`
  twice counts once) — see `scripts/calibration_android.py` gate.
- uiautomator cannot see Flutter-rendered text (single SurfaceView);
  pairing used coordinates from the one working dump + screenshots.
- No workload ceilings breached on Linux (RSS < 100 MB, wall < 30 s);
  disk stayed ≥20 GB; no thermal/ANR events (host has no thermal zone
  exposure; emulator ran concurrently — CPU spread documented, unfiltered).

## Unavailable metrics (standing)

GPU (both), Android CPU%, Linux thermal/battery-temp/low-power,
network QoS, per-field timestamps. Never faked; absent from records.

## Limitations and bias

Single laptop + single emulator; light/moderate only (no stress);
12-core dilution mutes single-thread loads; emulator variance includes
host load; N per cell caps statistical claims; agreement≠optimality.

## Readiness

Measured-evidence leg (Night 8): 5 cells × 30 suitable observations
(linux idle/light/moderate + android idle/light). Still required:
derivation memo, ranker-divergence report, fault/rollback logs, gap
acceptances (Android CPU/GPU, network QoS, tombstone memory-mode).
Governor v2 NOT READY until those complete.
