FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
COPY relay ./relay
RUN pip install --no-cache-dir .

CMD ["python", "-m", "relay.worker"]
