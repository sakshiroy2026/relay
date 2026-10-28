# One container: the API and the workers together, for free hosts that run a
# single web service and no separate background workers (Render's free plan,
# see learn/DEPLOY_RENDER.md). Postgres stays on Neon: DATABASE_URL,
# DEV_TENANT_ID and PUBLIC_DEMO_KEY come from the host's environment settings,
# never from this file.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # the public demo runs the free scripted model; slow it down so a visitor
    # can watch the steps stream in AND has a ~10 s window to press "Kill the
    # worker" before the run finishes (a run is 4 model calls: at 0.5 s it was
    # over in ~7 s, before most people find the button)
    FAKE_DELAY_SECONDS=2.5 \
    # demo lease: after "Kill the worker" the takeover waits for the lease to
    # expire, and nobody watches a 90 s pause. 15 s is the chaos harness's
    # test lease; safe here because the fake model answers in ~0.5 s. Remove
    # this line (back to 90 s) the day the service runs live paid models.
    LEASE_SECONDS=15

RUN useradd -m -u 1000 relay

WORKDIR /app

COPY pyproject.toml ./
COPY relay ./relay
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY deploy/single/start.sh ./start.sh

USER relay
EXPOSE 10000
CMD ["bash", "start.sh"]
