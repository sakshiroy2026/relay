FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
COPY relay ./relay
RUN pip install --no-cache-dir .

# Deployment: migrations (alembic), the seed / DLQ / check scripts, and the
# harness helpers the live smoke test uses. Run them with
# `docker compose run --rm api <command>` from /app.
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY chaos/harness.py ./chaos/

CMD ["python", "-m", "relay.worker"]
