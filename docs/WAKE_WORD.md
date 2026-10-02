# Wake Word: "Hey Zara" (`core/voice.py` — `WakeWordEngine`)

The phrase is EXACTLY **"Hey Zara"**. `WakeConfig` rejects anything else at
construction. One constant (`WAKE_PHRASE` core / `wakePhrase` Flutter) — no
duplicates to drift.

## Engine contract

`start | stop | pause(reason) | resume | battery_update(...)`. Real engines
must run a lightweight on-device detector; audio stays local until detection;
only post-activation audio enters the voice pipeline. `MockWakeEngine`
fires only via explicit `simulate_detection()` — deterministic for tests.

## Battery-first (configurable, no magic numbers)

`pause_below_pct = 15`, `resume_above_pct = 25`, `pause_on_power_save = true`.
Below threshold (and not charging) or in power-save: listening pauses,
state `paused-low-battery`, no LLM inference, no cloud audio. Recovers when
conditions improve. Never continuous cloud streaming while idle.

## Platform honesty

Android cannot guarantee true always-on low-power hotword without vendor
support (DSP/System hotword, Assistant-role privileges). The architecture
exposes `availability` and pauses cleanly where unsupported instead of
pretending. Physical audio validation (false-accept/reject rates, power draw)
is pending hardware and is NOT claimed.
