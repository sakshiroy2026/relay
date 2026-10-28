#!/usr/bin/env bash
# One-container entrypoint: migrate, start WORKERS workers, serve the API.
#
# GOES IN     DATABASE_URL, DEV_TENANT_ID, PUBLIC_DEMO_KEY (host env settings);
#             PORT (set by the host; Render uses 10000), WORKERS (default 2).
# COMES OUT   the API on $PORT + N worker processes.
# TOUCHES     Neon: `alembic upgrade head` (additive migrations), then the
#             workers' claim loop.
# FAILS WHEN  a migration fails or the API exits: the container stops and the
#             host restarts it. A worker that dies is restarted after 2 s; its
#             run is taken over by another worker once the lease expires.
set -euo pipefail

alembic upgrade head

for i in $(seq 1 "${WORKERS:-2}"); do
  (
    while true; do
      python -m relay.worker || true
      echo "worker $i exited; restarting in 2 s"
      sleep 2
    done
  ) &
done

exec uvicorn relay.api.main:app --host 0.0.0.0 --port "${PORT:-10000}" \
  --proxy-headers --forwarded-allow-ips '*'
