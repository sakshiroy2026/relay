from fastapi import FastAPI, Response

from relay.api.routes import runs
from relay.db import pool

app = FastAPI(title="Relay", version="0.1.0")
app.include_router(runs.router)


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
