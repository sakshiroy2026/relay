from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse

from relay.api.routes import companies, runs
from relay.db import pool

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Relay", version="0.1.0")
app.include_router(runs.router)
app.include_router(companies.router)


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
