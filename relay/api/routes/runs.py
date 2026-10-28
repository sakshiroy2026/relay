import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from relay.api import ratelimit, repo
from relay.api.principal import Principal, require_principal


def guarded(principal: Annotated[Principal, Depends(require_principal)]) -> Principal:
    """Auth, then the tenant's rate limit — the dependency every /v1 route shares."""
    ratelimit.check(principal)
    return principal


# every route on this router authenticates and is rate-limited (Day 10)
router = APIRouter(prefix="/v1", tags=["runs"])

Caller = Annotated[Principal, Depends(guarded)]


class CreateRunRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


class CreateRunResponse(BaseModel):
    run_id: UUID
    status: str


@router.post("/runs", status_code=202, response_model=CreateRunResponse)
def create_run(body: CreateRunRequest, principal: Caller) -> CreateRunResponse:
    try:
        created = repo.create_run(principal.tenant_id, body.domain, principal.max_concurrent_runs)
    except repo.ConcurrencyLimit as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many open runs: {exc.open_runs} pending/running, "
                f"cap {exc.cap}; wait for runs to finish"
            ),
            headers={"Retry-After": "5"},
        ) from exc
    return CreateRunResponse(run_id=created["run_id"], status=created["status"])


@router.get("/runs/{run_id}")
def get_run(run_id: UUID, principal: Caller) -> dict:
    row = repo.get_run(principal.tenant_id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


# ── read-only ledger view for the demo page ───────────────────────────────
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
    if kind == "repair_requested":
        return "final answer failed validation; asked the model to correct it"
    if kind == "requeued":
        return f"requeued by {p.get('requeued_by', 'an operator')} (was {p.get('from_status')})"
    return kind


@router.get("/runs/{run_id}/events")
def run_events(run_id: UUID, principal: Caller) -> StreamingResponse:
    """The run pushed as server-sent events (Day 11); the page falls back to polling."""
    from relay.api.events import run_event_stream

    return StreamingResponse(
        run_event_stream(principal.tenant_id, run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/steps")
def get_run_steps(run_id: UUID, principal: Caller) -> list[dict[str, Any]]:
    rows = repo.get_run_steps(principal.tenant_id, run_id)
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
