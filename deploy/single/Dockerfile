# One container: the API and the workers together, for free hosts that run a
# single web service and no separate background workers (Render's free plan,
# see learn/DEPLOY_RENDER.md). Postgres stays on Neon: DATABASE_URL,
# DEV_TENANT_ID and PUBLIC_DEMO_KEY come from the host's environment settings,
# never from this file.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # the public demo runs the free scripted model; slow it a little so a
    # visitor can watch the steps stream in (and a worker can die mid-run)
    FAKE_DELAY_SECONDS=0.5

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
