"""save_company under racing callers and a dead writer, against a real database.

The receipt book (relay/core/idempotency.py) is only interesting under
concurrency, so these tests make callers overlap on purpose: each write waits
inside its transaction until every racer has arrived, which guarantees they all
get past the receipt check before anyone commits. (A fixed sleep did not: under
load one racer could arrive after another had committed, and never write at all.)
"""

import json
import threading
import uuid
from typing import Any

import psycopg
import pytest

import relay.agent.tools as tools_mod
from relay.agent.fake_script import ENRICH_SCRIPT
from relay.agent.tools import ToolContext, dispatch_tool

RECORD = ENRICH_SCRIPT[2].tool_calls[0].args  # the fake model's save_company args
RACERS = 4  # the pool has 5 connections


def receipt(key: str) -> dict[str, Any]:
    from relay.db import pool

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, attempts, executions FROM tool_invocations WHERE idem_key = %s",
            (key,),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "attempts": row[1], "executions": row[2]}


def company_rows(domain: str) -> int:
    from relay.db import pool

    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM companies WHERE domain = %s", (domain,)).fetchone()
    return 0 if row is None else int(row[0])


@pytest.fixture
def widened(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Hold every save_company write inside its transaction until all racers are there."""
    real = tools_mod.run_once
    started: list[int] = []
    all_here = threading.Barrier(RACERS)

    def counting(key: str, run_id: Any, idx: int, name: str, execute: Any) -> Any:
        def wrapped(cur: psycopg.Cursor[Any]) -> dict[str, Any]:
            started.append(1)
            all_here.wait(timeout=20)
            result: dict[str, Any] = execute(cur)
            return result

        return real(key, run_id, idx, name, wrapped)

    monkeypatch.setattr(tools_mod, "run_once", counting)
    return started


def test_racing_callers_execute_once(parked_run: tuple[uuid.UUID, str], widened: list[int]) -> None:
    run_id, domain = parked_run
    ctx = ToolContext(run_id=run_id, domain=domain, step_index=1, idem_key=f"{run_id}:race")
    barrier = threading.Barrier(RACERS)
    answers: list[dict[str, Any]] = []

    def race() -> None:
        barrier.wait()
        out = dispatch_tool("save_company", RECORD, ctx)
        assert out.ok, out.content
        answers.append(json.loads(out.content))

    threads = [threading.Thread(target=race) for _ in range(RACERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(answers) == RACERS
    assert all(a == answers[0] for a in answers)  # everyone is told the same thing
    assert receipt(ctx.idem_key) == {"status": "ok", "attempts": RACERS, "executions": 1}
    assert company_rows(domain) == 1
    assert len(widened) == RACERS  # all 4 really wrote; 3 were rolled back by the receipt


def test_writer_killed_mid_transaction_leaves_nothing(parked_run: tuple[uuid.UUID, str]) -> None:
    """A worker dies after recording the key and while its write is uncommitted."""
    from relay.config import settings
    from relay.db import pool

    run_id, domain = parked_run
    key = f"{run_id}:dead_writer"
    with pool.connection() as conn:  # step 1 of run_once, as the dead worker did it
        conn.execute(
            "INSERT INTO tool_invocations (idem_key, run_id, step_index, tool_name, status) "
            "VALUES (%s, %s, 1, 'save_company', 'in_flight')",
            (key, run_id),
        )
    dead = psycopg.connect(settings.database_url)  # its own socket, like a separate process
    dead.execute(
        "INSERT INTO companies (tenant_id, domain, name) VALUES (%s, %s, 'half-written')",
        (settings.dev_tenant_id, domain),
    )
    dead.close()  # the socket closes without COMMIT: Postgres rolls the write back
    assert company_rows(domain) == 0

    ctx = ToolContext(run_id=run_id, domain=domain, step_index=3, idem_key=key)
    out = dispatch_tool("save_company", RECORD, ctx)  # the next worker re-issues the call
    assert out.ok, out.content
    assert receipt(key) == {"status": "ok", "attempts": 2, "executions": 1}
    assert company_rows(domain) == 1
