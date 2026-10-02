# Battery / Resource Model (`core/governor.py`)

Battery awareness is first-class, especially Android.

## Never

- Continuous screen capture / camera processing / heavy on-device AI /
  tight polling loops / needless foreground services.

## Prefer

Event-driven operation, push, Android wake mechanisms, adaptive sensing,
server-side processing, laptop/cloud for heavy tasks, deferral on low battery.

## Governor inputs (`ResourceSnapshot`)

`battery_pct`, `charging`, `network` (`offline|metered|wifi|wired`),
`device_kind`, `cpu_pressure`, `low_power_mode`.

## Decisions (`GovernorDecision`: `proceed|defer|reroute`)

- Battery < 15% and not charging → `defer` all cost > 0.2; only `safe`
  reads allowed locally.
- Battery < 30% → cap cost at 0.5 unless charging.
- Metered/offline → defer uploads, model downloads, bulk sync; queue them.
- Heavy task (`estimated_cost` high) on phone → `reroute` to laptop/cloud
  when a capable device is online (decision returned to Device Manager,
  which owns routing).
- Oracle Free-tier guard (Stage 4 wires real meters): storage/DB/log caps
  with retention + cleanup policies; no paid service mandatory.

## Offline (`devices.py` + `missions.py`)

Offline is a state, not an error: keep minimal local state, accept/queue
commands, run only lightweight permitted local actions, preserve mission
checkpoints, queue sync, resume on `device_online` / `network_changed`
events. Heavy work waits for network, laptop, cloud, or charging.
