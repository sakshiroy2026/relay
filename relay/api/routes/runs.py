from uuid import UUID

from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from relay.config import settings
from relay.db import pool

router = APIRouter(prefix="/v1", tags=["runs"])

AGENT_VERSION = "enrich@v1"


class CreateRunRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


class CreateRunResponse(BaseModel):
    run_id: UUID
    status: str


@router.post("/runs", status_code=202, response_model=CreateRunResponse)
def create_run(body: CreateRunRequest) -> CreateRunResponse:
    payload = {"domain": body.domain}

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (tenant_id, input, agent_version)
                VALUES (%s, %s, %s)
                RETURNING id, status
                """,
                (settings.dev_tenant_id, Jsonb(payload), AGENT_VERSION),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=500, detail="run insert returned no row")
            run_id, status = row

            cur.execute(
                """
                INSERT INTO steps (run_id, step_index, kind, payload, written_by)
                VALUES (%s, %s, 'run_started', %s, %s)
                """,
                (run_id, 0, Jsonb(payload), "api"),
            )

    return CreateRunResponse(run_id=run_id, status=status)


@router.get("/runs/{run_id}")
def get_run(run_id: UUID) -> dict:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, status, input, result, error,
                       attempt_count, next_step_index,
                       spent_usd, tokens_in, tokens_out,
                       agent_version, created_at, started_at, finished_at
                FROM runs
                WHERE id = %s AND tenant_id = %s
                """,
                (run_id, settings.dev_tenant_id),
            )
            row = cur.fetchone()

            if row is None:
                raise HTTPException(status_code=404, detail="run not found")

    return row
