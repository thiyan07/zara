# Deployment (free-first; NOT yet deployed to Oracle)

Status: IMPLEMENTED artifacts, local-run validated. Docker daemon is not
available on this machine, so image build is NOT VALIDATED; Oracle
deployment is DEFERRED (manual cloud step documented below).

## Local run (validated)

```bash
pip install -r requirements.txt
ASSISTANT_TOKEN=<random> ZARA_MEMORY_DB=assistant.db \
ZARA_MISSION_DB=missions.db ZARA_AUDIT_DB=audit.db \
  python -m uvicorn core.app:app --port 8080
curl localhost:8080/v1/health   # liveness
curl localhost:8080/v1/ready    # readiness (registry/memory/bus/provider)
```

Backups: `POST /v1/admin/backup` (operator token) writes timestamped SQLite
copies to `./backups/`. Restore is a copy to a NEW path
(`core/backup.py::restore_sqlite` refuses to overwrite).

## Container (image NOT built here — no docker daemon)

`Dockerfile` (slim, non-root `zara` user, healthcheck on `/v1/health`) +
`docker-compose.yml` (1 CPU / 1 GB limit, restart policy, named volume for
`/data`, healthcheck on `/v1/ready`). Secrets via `.env` (see `.env.example`);
never baked into the image.

## Oracle Always Free (manual steps, NOT performed)

1. Create Always Free Ampere VM (1 OCPU/6 GB or 4 OCPU/24 GB split) +
   VCN with ingress 443 only (plus 22 from your IP).
2. Install docker, copy repo, create `.env` with a random ASSISTANT_TOKEN.
3. `docker compose up -d --build`; verify `/v1/health` + `/v1/ready` via
   HTTPS (reverse proxy + Let's Encrypt on the host; the app itself serves
   plain HTTP on localhost only).
4. PostgreSQL + pgvector later: same schema (see `core/db.py` notes); SQLite
   files remain the default until then.
5. Set a calendar check on free-tier usage; the app exposes no paid
   dependency, but Oracle quotas are Oracle's — no "always free" guarantee
   is hardcoded anywhere.

Resource posture: SQLite default, bounded queues/caches (100/job-queue,
100/WS-queue, 200 offline events, 60 msgs/session), no background model
downloads, Playwright/Chrome only where installed.
