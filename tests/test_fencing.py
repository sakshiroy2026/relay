"""Fencing against a real database: what stops a worker that lost its lease.

Three guards, one test each: the UNIQUE(run_id, step_index) constraint (whoever
writes an index first wins, the other gets LeaseLost), the heartbeat at the top
of every turn, and the guarded finish in relay/worker.py. None of these tests
claims runs: the claim query takes any pending run, and a shared database may
hold real ones.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import relay.worker as worker_mod
from relay.agent.loop import run_agent
from relay.core.ledger import LeaseLost, append_step
from scripts.check_run import find_problems, load_steps


def steps_of(run_id: uuid.UUID) -> list[dict[str, Any]]:
    return load_steps(str(run_id))


def test_second_writer_of_an_index_is_fenced(parked_run: tuple[uuid.UUID, str]) -> None:
    """Either worker can win the race for step 1; the loser stops with LeaseLost."""
    run_id, _ = parked_run
    append_step(run_id, 1, "error", {"reason": "winner"}, "worker-a")
    with pytest.raises(LeaseLost, match="step 1 of run"):
        append_step(run_id, 1, "error", {"reason": "loser"}, "worker-b")
    rows = steps_of(run_id)
    assert [(r["step_index"], r["written_by"]) for r in rows] == [(0, "pytest"), (1, "worker-a")]


def test_heartbeat_false_stops_before_the_model_is_paid(parked_run: tuple[uuid.UUID, str]) -> None:
    run_id, domain = parked_run
    with pytest.raises(LeaseLost, match="before turn 0"):
        run_agent(run_id, domain, "zombie", lambda: False, budget_usd=1.0)
    assert len(steps_of(run_id)) == 1  # nothing written after run_started


def test_zombie_frozen_after_its_heartbeat_loses_its_next_write(
    parked_run: tuple[uuid.UUID, str],
) -> None:
    """The window the heartbeat can't close: frozen right AFTER a successful heartbeat.

    While the zombie is frozen, the new owner writes the next step. The zombie
    wakes, finishes its (paid) model call, tries to write it at the same index,
    and is fenced by the constraint. The ledger keeps the new owner's row.
    """
    run_id, domain = parked_run
    beats = 0

    def heartbeat() -> bool:
        nonlocal beats
        beats += 1
        if beats == 2:  # top of turn 1: "frozen" here; meanwhile the new owner writes step 4
            from relay.db import pool

            with pool.connection() as conn:
                conn.execute(
                    "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                    "VALUES (%s, 4, 'error', %s, 'new-owner')",
                    (run_id, Jsonb({"reason": "stand-in for the new owner's step"})),
                )
        return True  # the heartbeat itself succeeded: the lease was still ours

    with pytest.raises(LeaseLost, match=r"step 4 of run"):
        run_agent(run_id, domain, "zombie", heartbeat, budget_usd=1.0)

    rows = steps_of(run_id)
    assert [r["written_by"] for r in rows] == ["pytest", "zombie", "zombie", "zombie", "new-owner"]
    assert find_problems(rows) == []  # turn 0 by the zombie, then the new owner's row


@pytest.fixture
def leased_run(parked_run: tuple[uuid.UUID, str]) -> Iterator[uuid.UUID]:
    """A running run whose lease belongs to 'new-owner' for the next hour."""
    from relay.db import pool

    run_id, _ = parked_run
    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET status = 'running', lease_owner = 'new-owner', "
            "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
            (run_id,),
        )
    yield run_id
    with pool.connection() as conn:  # park it again so no worker ever picks it up
        conn.execute("UPDATE runs SET status = 'dead' WHERE id = %s", (run_id,))


def test_guarded_finish_refuses_a_zombie(
    leased_run: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relay.db import pool

    monkeypatch.setattr(worker_mod, "WORKER_ID", "zombie")
    assert worker_mod.extend_lease(leased_run) is False
    assert worker_mod.mark_succeeded(leased_run, {"name": "stale"}) is False
    assert worker_mod.mark_failed(leased_run) is False

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status::text, lease_owner, result FROM runs WHERE id = %s", (leased_run,)
        ).fetchone()
    assert row == ("running", "new-owner", None)  # untouched

    monkeypatch.setattr(worker_mod, "WORKER_ID", "new-owner")
    assert worker_mod.extend_lease(leased_run) is True
