# RESOURCE TELEMETRY — foundation (observation only, no scheduler)

**Status: IMPLEMENTED. Telemetry foundation complete; scheduler weights
intentionally deferred.**

## 1. ResourceSnapshot schema

`core/governor.py`: battery_pct/charging/network/device_kind (existing) +
`cpu_pressure` (legacy, now `None` default, governor-ignored) +
`low_power_mode/cpu_pct/mem_pct/storage_free_mb/battery_temp_c/
thermal_state` (all `None` default = unknown). Only `low_power_mode`
feeds the pre-existing gate; everything else is read-only context.

## 2. Telemetry sources

| Signal | Android | Linux | Core field |
|---|---|---|---|
| CPU usage % | UNAVAILABLE (no public API; procfs banned) → `null` | loadavg/cores approximation, labeled | `cpu_pct` |
| CPU count | `Runtime.availableProcessors()` (capacity, not usage) | `os.cpu_count()` | telem map only |
| RAM | `ActivityManager.MemoryInfo` avail/total → device-side `mem_pct` | `/proc/meminfo` | `mem_pct` |
| Storage | `StatFs` data dir avail/total | `shutil.disk_usage` | `storage_free_mb/_total_mb` |
| Battery temp | `EXTRA_TEMPERATURE` | n/a (desktop) | `battery_temp_c` |
| Thermal | `PowerManager` API 29+, guarded; older omit | n/a | `thermal_state` |
| Power-save | `isPowerSaveMode` (was collected, now sent) | n/a | `low_power_mode` |
| GPU | UNAVAILABLE (no stack; documented, not faked) | UNAVAILABLE | — (no field) |

Flow: device heartbeat → `HeartbeatPayload` (strict bounds,
`allow_inf_nan=False`) → `DeviceManager.heartbeat` (None-preserving,
server-stamped `last_telemetry_at`) → `DeviceState` →
`_snapshot_for`/routing/app snapshots → `FabricDeviceView.as_dict`.

## 3. Freshness rules

- `last_seen`: server receipt of any contact (presence clock, `stale_s=120`).
- `last_telemetry_at`: server receipt of a heartbeat carrying ≥1
  non-None telemetry field. `telemetry_stale(dev, 120 s)` helper.
- Bare polls advance presence only, never touch telemetry.
- Stale telemetry stays readable but readers must treat it as unknown.
- No device clock trusted; no second clock abstraction.

## 4. Validation

Pydantic bounds on every field (`ge/le`, `max_length`, finite floats);
network normalized (strip/lower/≤32 chars); thermal ≤16 chars; unknown
extras ignored both directions (no `extra="forbid"` on heartbeat).
Malformed → 422, stored values untouched. Old bodies (battery-only)
validate unchanged.

## 5-8. Semantics

- CPU: percent 0–100; loadavg-derived on Linux (documented approximation);
  Android reports `null` (honest).
- RAM: `used = total − available`; `0 ≤ avail ≤ total` enforced
  device-side (Android) and stored as `mem_pct`.
- Battery: existing telemetry reused; no redesign. `low_power_mode`
  `None` (not `False`) when unreported.
- Storage: free/total MB, no scans. GPU: unavailable, no field, not
  faked. Thermal: enum string or `null`; unknown on old APIs.

## 9. Resource demand vector

`ToolDefinition.resource_demands: ResourceDemands` — 4 tiers ×
{low, moderate, high} (81 combos), all-LOW default (no migration),
closed model (unknown dims/values rejected). Description only; no
gate reads it. `estimated_cost` scalar untouched.

## 10. Trust/security model

TELEMETRY IS OBSERVATION, NOT AUTHORITY. Battery/network/cpu never
decide identity, trust, policy, approval, or routing targets. Governor
defers only on explicitly valid adverse readings; unknown/NaN/out-of-
range never defer and never boost. Audit metadata only. `agent_result`
device-bound; poll presence ≠ telemetry freshness.

## 11. Backward compatibility

All new fields optional-with-default; old clients validate; new-client
→old-core ignores extras; old snapshot callers unaffected; Stage 14–19
behavior unchanged (738-suite green).

## 17. Telemetry expansion delta (Task 3)

- `storage_total_mb` now flows heartbeat → store → snapshot → view
  (previously collected but dropped at the snapshot).
- Stale telemetry nulled at `resource_snapshot` build (battery/network
  keep legacy presence-adjacent behavior); `telemetry_stale()` is now
  consumed in production, not just tests.
- Validation hardening: storage caps (10⁷ MB), thermal closed enum
  (else dropped), free>total drops both, second-layer clamp in
  `DeviceManager.heartbeat` for non-HTTP writers, bool-is-not-a-number.
- Linux: true CPU% via `/proc/stat` delta (labeled `proc_stat`,
  loadavg fallback labeled); battery temp via `BAT*/temp` best-effort.
  No psutil (stdlib suffices; psutil adds no GPU capability).
- Android: unchanged this task (already complete). CPU% permanently
  unavailable (no public API); GPU unavailable everywhere.

## 12. Measured emulator values (ZaraLab API 36, 2026-10-08)

`mem_pct` 78.3–79.4 · `low_power_mode` false · `storage` 8276/9928 MB ·
`battery_temp_c` 25.0 · `thermal_state` none · `cpu_pct` null (documented
API gap) · `last_telemetry_at` fresh per 30 s tick.

## 13. Measured Linux values (2026-10-08, 12-core laptop)

`cpu_count` 12 · `cpu_pct` 8.5 (loadavg-derived) · `mem` 2994/14788 MB
(79.8%) · `storage` 22950/126454 MB · GPU/thermal unavailable.

## 14. Calibration observations

Idle mem ~78.96% → ~79.41% after 60 throttled Settings monkey events;
thermal none, temp 25.0 °C stable. Plausible live response; NOT used for
any weight (single light sample, no scheduler input).

## 15. Scheduler work intentionally deferred

No ranking weights, no scores, no CPU/RAM tradeoff, no battery penalty,
no GPU preference, no migration, no constraint language. Demand tiers
unread by every gate (verified: no reader exists).

## 16. Remaining prerequisites for Governor v2

Central snapshot provider (4 builders) → demand-aware thresholds (not
scores) → user-constraint schema → single scheduler → full telemetry
(GPU/RAM-detail/latency) → emulator calibration set.
