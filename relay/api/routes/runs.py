import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from relay.api.auth import require_api_key
from relay.config import settings
from relay.db import pool

# every route on this router needs the API key (safety slice)
router = APIRouter(prefix="/v1", tags=["runs"], dependencies=[Depends(require_api_key)])

AGENT_VERSION = "enrich@v2"  # v2 (Day 7): prompt lists the internal tools; saves before answering


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
                       attempt_count, next_step_index, lease_owner,
                       spent_usd, budget_usd, tokens_in, tokens_out,
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


# ── read-only ledger view for the demo page (Stage 4) ────────────────────
SUMMARY_CHARS = 140  # page texts are long; the page only needs a glance


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


def summarize_step(kind: str, p: dict[str, Any]) -> str:
    """One human line per ledger row. Never returns full page texts."""
    if kind == "run_started":
        return f"run started for {p.get('domain')}"
    if kind == "model_call":
        asked = ", ".join(tc["name"] for tc in p.get("tool_calls", []))
        head = f"asks for {asked}" if asked else "final answer"
        return f"{head} · ${p.get('cost_usd', 0):.6f} · {_short(p.get('content', ''))}"
    if kind == "tool_call":
        return f"{p.get('tool')} {_short(json.dumps(p.get('args', {})))}"
    if kind == "tool_result":
        status = "ok" if p.get("ok") else "failed"
        return f"{p.get('tool')} {status}: {_short(str(p.get('content', '')))}"
    if kind == "final":
        return f"validated record: {p.get('name')}"
    if kind == "error":
        return f"error: {p.get('reason')}"
    if kind == "budget_exceeded":
        return f"budget exceeded: spent ${p.get('spent_usd')} of ${p.get('budget_usd')}"
    return kind


@router.get("/runs/{run_id}/steps")
def get_run_steps(run_id: UUID) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.step_index, s.kind, s.written_by, s.created_at, s.payload
                FROM steps s
                JOIN runs r ON r.id = s.run_id
                WHERE s.run_id = %s AND r.tenant_id = %s
                ORDER BY s.step_index
                """,
                (run_id, settings.dev_tenant_id),
            )
            rows = cur.fetchall()

    return [
        {
            "step_index": r["step_index"],
            "kind": r["kind"],
            "written_by": r["written_by"],
            "created_at": r["created_at"],
            "summary": summarize_step(r["kind"], r["payload"]),
        }
        for r in rows
    ]
