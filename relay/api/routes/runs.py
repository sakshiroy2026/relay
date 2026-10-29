import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
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
    # P7, Option A: a public visitor opting one run into the real (free-tier)
    # model. Demo-mode tenants only — a live tenant's runs are already real.
    real_ai: bool = False


class CreateRunResponse(BaseModel):
    run_id: UUID
    status: str
    mode: str = "demo"  # what this run actually got: demo / public_real / live
    real_ai_runs_left: int = 0  # the shared allowance left today, after this run
    note: str | None = None  # plain English when the mode is not what was asked for


# A client key is any 1-255 printable-ASCII characters (a UUID is typical).
IDEMPOTENCY_KEY_MAX = 255


@router.post("/runs", status_code=202, response_model=CreateRunResponse)
def create_run(
    body: CreateRunRequest,
    principal: Caller,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateRunResponse:
    """Create a run. With an Idempotency-Key, a retried POST returns the same run.

    Same key + same body -> the original run (header `Idempotent-Replayed: true`),
    even when the tenant is at its concurrency cap. Same key + different body -> 422.

    `real_ai` (P7) opts one run into the real free-tier model. It is a
    demo-tenant feature: a live tenant already runs the real model, so asking
    is a client bug and answered 409 rather than silently ignored. Once the
    day's shared allowance is spent the run is still created — on the free
    scripted model — and `mode` / `note` say so. While another visitor's
    real-AI run is in flight the answer is 429 with `Retry-After`, and nothing
    is created: the free tier's per-minute quota fits one real run at a time.
    """
    if idempotency_key is not None and not (
        0 < len(idempotency_key) <= IDEMPOTENCY_KEY_MAX
        and idempotency_key.isascii()
        and idempotency_key.isprintable()
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Idempotency-Key must be 1-{IDEMPOTENCY_KEY_MAX} printable ASCII characters",
        )
    if body.real_ai and principal.run_mode != "demo":
        raise HTTPException(
            status_code=409,
            detail=(
                "real_ai is the public demo's opt-in; this tenant is "
                f"{principal.run_mode}-mode and already runs the real model"
            ),
        )
    try:
        created = repo.create_run(
            principal.tenant_id,
            body.domain,
            principal.max_concurrent_runs,
            idempotency_key,
            principal.run_mode,
            real_ai=body.real_ai,
        )
    except repo.IdempotencyMismatch as exc:
        raise HTTPException(
            status_code=422,
            detail="this Idempotency-Key was already used with a different request body",
        ) from exc
    except repo.RealAIBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                "another visitor's real-AI run is still in flight; the free tier "
                "has room for one at a time — try again in a few seconds"
            ),
            headers={"Retry-After": "10"},
        ) from exc
    except repo.ConcurrencyLimit as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                f"too many open runs: {exc.open_runs} pending/running, "
                f"cap {exc.cap}; wait for runs to finish"
            ),
            headers={"Retry-After": "5"},
        ) from exc
    if created["replayed"]:
        response.headers["Idempotent-Replayed"] = "true"
    note = None
    if body.real_ai and created["mode"] != "public_real":
        note = (
            "today's real-AI runs are used up; this run uses the demo model "
            "(the allowance resets at midnight US Pacific)"
        )
    return CreateRunResponse(
        run_id=created["run_id"],
        status=created["status"],
        mode=created["mode"],
        real_ai_runs_left=created["real_ai_runs_left"],
        note=note,
    )


@router.post("/runs/{run_id}/crash", status_code=202)
def crash_run(run_id: UUID, principal: Caller) -> dict:
    """Demo: kill the worker that owns this run, live, so a visitor can watch
    another worker resume it from the ledger.

    The flag is read at the worker's next heartbeat; the worker clears it and
    exits like `docker kill` (no cleanup). Demo-mode tenants only, so a crash
    can never waste a paid call: their runs are the free scripted model, or
    (P7) the real model on Google's free tier, where a resumed run re-asks at
    $0 and costs only a few of the day's free requests.
    """
    if principal.run_mode != "demo":
        raise HTTPException(
            status_code=403, detail="crash-the-worker is a demo feature; this tenant is live-mode"
        )
    outcome = repo.request_crash(principal.tenant_id, run_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="run not found")
    if outcome == "not_running":
        raise HTTPException(status_code=409, detail="run is not running; nothing to kill")
    return {"status": "crash_requested", "note": "the owning worker dies at its next heartbeat"}


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
