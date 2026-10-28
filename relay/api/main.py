import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from relay.api.routes import companies, runs
from relay.db import pool
from relay.obs import metrics
from relay.obs.tracing import setup as tracing_setup

STATIC_DIR = Path(__file__).parent / "static"

tracing_setup("relay-api")
metrics.register()

app = FastAPI(title="Relay", version="0.1.0")
app.include_router(runs.router)
app.include_router(companies.router)


@app.middleware("http")
async def observe_requests(request: Request, call_next: Any) -> Any:
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    metrics.HTTP_LATENCY.labels(
        request.method,
        getattr(route, "path", "unmatched"),  # the template (/v1/runs/{run_id}), not the raw path
        str(response.status_code),
    ).observe(time.perf_counter() - started)
    return response


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    # counts and states only, no payloads or keys; open like /healthz
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/ops", include_in_schema=False)
def ops_page() -> FileResponse:
    # the ops dashboard: static HTML polling /ops.json; no key inside
    return FileResponse(STATIC_DIR / "ops.html", media_type="text/html")


@app.get("/ops.json", include_in_schema=False)
def ops_json() -> dict:
    return metrics.snapshot()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # the demo page: static HTML, no key inside; it asks the viewer for one
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response) -> dict:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as exc:
        response.status_code = 503
        return {"status": "unavailable", "db": str(exc)}

    return {"status": "ok", "db": "ok"}
