"""Day 11: the SSE stream, driven directly (the generator) and over ASGI (terminal runs).

A mid-stream disconnect through the TestClient portal can deadlock on the
blocked sync generator, so the live-run case drives run_event_stream itself —
each pull blocks at most TICK_SECONDS.
"""

import uuid
from collections.abc import Iterator

import pytest
from psycopg.types.json import Jsonb

from relay.api.events import run_event_stream
from relay.config import settings


def insert_step(run_id: uuid.UUID, index: int) -> None:
    from relay.db import pool

    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
            "VALUES (%s, %s, 'tool_call', %s, 'sse-test')",
            (run_id, index, Jsonb({"tool": "web_search", "args": {}, "tool_call_id": "c1"})),
        )


@pytest.fixture
def live_run(parked_run: tuple[uuid.UUID, str]) -> Iterator[uuid.UUID]:
    from relay.db import pool

    run_id, _ = parked_run
    # 'running' with a live lease: SSE-visible but never claimable by a worker
    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET status = 'running', lease_owner = 'sse-test', "
            "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
            (run_id,),
        )
    yield run_id
    with pool.connection() as conn:
        conn.execute("UPDATE runs SET status = 'dead' WHERE id = %s", (run_id,))


def events_of(block: str) -> tuple[str, str]:
    lines = block.strip().splitlines()
    event = next((ln[7:] for ln in lines if ln.startswith("event: ")), "")
    data = next((ln[6:] for ln in lines if ln.startswith("data: ")), "")
    return event, data


def test_stream_pushes_a_new_step_without_a_new_request(live_run: uuid.UUID) -> None:
    tenant = uuid.UUID(settings.dev_tenant_id)
    gen = run_event_stream(tenant, live_run)
    try:
        event, data = events_of(next(gen))
        assert event == "update" and '"run_started"' in data  # the snapshot, immediately

        insert_step(live_run, 1)  # the trigger rings; the next pull must carry the step
        for _ in range(4):  # a tick comment may interleave; a few pulls at most
            block = next(gen)
            if block.startswith("event:"):
                break
        event, data = events_of(block)
        assert event == "update" and '"tool_call"' in data
    finally:
        gen.close()  # releases the LISTEN connection


def test_terminal_run_gets_update_then_done_and_the_stream_ends(
    parked_run: tuple[uuid.UUID, str],
) -> None:
    run_id, _ = parked_run  # status 'dead' = terminal
    gen = run_event_stream(uuid.UUID(settings.dev_tenant_id), run_id)
    blocks = list(gen)  # the generator must return on its own
    events = [events_of(b)[0] for b in blocks if b.startswith("event:")]
    assert events == ["update", "done"]


def test_another_tenants_run_is_gone(parked_run: tuple[uuid.UUID, str]) -> None:
    run_id, _ = parked_run
    gen = run_event_stream(uuid.uuid4(), run_id)  # a tenant that doesn't own it
    event, _ = events_of(next(gen))
    assert event == "gone"
    assert list(gen) == []  # and nothing more


def test_sse_route_requires_a_key(db: None) -> None:
    from fastapi.testclient import TestClient

    from relay.api.main import app

    r = TestClient(app).get(f"/v1/runs/{uuid.uuid4()}/events")
    assert r.status_code == 401


def test_notify_trigger_rings_on_insert(parked_run: tuple[uuid.UUID, str]) -> None:
    import psycopg

    run_id, _ = parked_run
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("LISTEN relay_steps")
        insert_step(run_id, 1)
        got = [n.payload for n in conn.notifies(timeout=5, stop_after=1)]
    assert got == [str(run_id)]
