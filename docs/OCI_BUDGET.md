# OCI Always Free resource budget — ESTIMATES, not measured results

Status: planning estimates for a single-instance deployment. Nothing below
has been measured on OCI; no OCI resources have been provisioned in this
run. Validate with the observation commands before trusting any number.

## Target shape

- 1× Ampere A1 OCPU (or 1–2 x86 OCPUs), **2–4 GB RAM** comfortable;
  fits the 4-OCPU/24 GB Always Free envelope with headroom.
- Single FastAPI worker (`--workers 1`, `--limit-concurrency 64`,
  `--limit-max-requests 2000`, keep-alive 15 s — see `Dockerfile`).
- SQLite files on a persistent volume (default; PostgreSQL optional later).
- No STT/TTS/LLM inference on the VM (laptop/phone or explicit opt-in
  providers only). No paid services, no K8s, no Redis, no vector DB.

## Estimates (label: ESTIMATE)

| Resource | Estimate | Basis |
|----------|----------|-------|
| Idle RAM | ~150–300 MB | Python + FastAPI + SQLite, one worker |
| Busy RAM | ~400–800 MB | Concurrent requests + bounded queues (100/job, 100/WS, 200 events, 60 msgs/session) |
| CPU idle | ~0–2% of 1 OCPU | Poll-free timers, no background loops |
| CPU per request | milliseconds | JSON + SQLite; STT/TTS/LLM are off-VM |
| Disk (app + deps) | ~300–500 MB | slim image, no models in image |
| Disk (data growth) | ~MBs/month | SQLite rows are small; audio never persisted |
| Backups | ~same as data dir | timestamped SQLite copies via `POST /v1/admin/backup` |
| Network | low | text + task metadata; 2 MiB max audio body, 64 KiB transfer chunks |

Heavy local-only pieces that must NEVER ride the VM image: faster-whisper
models (~75 MB+), Piper voices (~60 MB+), Playwright browsers (~150 MB+),
Flutter/Gradle caches (GBs).

## Observe actual use (run on the VM)

```bash
free -m                       # RAM
docker stats --no-stream zara # container CPU/RAM
du -sh /data /srv/zara/backups # data + backup growth
df -h /                        # disk headroom (keep >= 20% free)
curl localhost:8080/v1/health # liveness
curl -H "Authorization: Bearer $ASSISTANT_TOKEN" \
  localhost:8080/v1/ready     # readiness (registry/memory/bus)
```

## Guardrails already in code

- Global body cap `ZARA_MAX_BODY_BYTES` (default 5 MiB, 413 over) +
  per-endpoint audio cap (2 MiB).
- Rate limits: 600/min general, 30/min sensitive (enroll/claim) per client.
- Bounded queues/caches/sessions; bounded scheduler ledger (200).
- Non-root `zara` user; secrets via `.env` only (never in image/logs).
- TLS + firewall (443 + scoped 22 only) are host-level manual steps —
  see `docs/DEPLOYMENT.md`. The app serves plain HTTP on localhost only.
