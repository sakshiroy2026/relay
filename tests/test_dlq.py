"""The dead-letter queue against a real database: reaping and requeueing.

Requeued runs are briefly 'pending', which a live worker could claim; each test
parks its run again at the end, and the runs carry throwaway domains either way.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from relay.core.dlq import REQUEUE_EXTRA_ATTEMPTS, reap_exhausted, requeue_run


def set_run(run_id: uuid.UUID, **cols: Any) -> None:
    from relay.db import pool

    sets = ", ".join(f"{k} = %s" for k in cols)
    with pool.connection() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id = %s", (*cols.values(), run_id))


def run_row(run_id: uuid.UUID) -> dict[str, Any]:
    from psycopg.rows import dict_row

    from relay.db import pool

    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT status::text AS status, attempt_count, max_attempts, error, "
            "lease_owner, budget_usd FROM runs WHERE id = %s",
            (run_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def step_kinds(run_id: uuid.UUID) -> list[str]:
    from relay.db import pool

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind::text FROM steps WHERE run_id = %s ORDER BY step_index", (run_id,)
        ).fetchall()
    return [r[0] for r in rows]


@pytest.fixture
def reparked(parked_run: tuple[uuid.UUID, str]) -> Iterator[uuid.UUID]:
    run_id, _ = parked_run
    yield run_id
    set_run(run_id, status="dead")  # never leave a claimable test run behind


def test_reaper_dead_letters_an_exhausted_run(reparked: uuid.UUID) -> None:
    run_id = reparked
    set_run(run_id, status="running", attempt_count=5, max_attempts=5, lease_owner="rip")
    from relay.db import pool

    with pool.connection() as conn:  # lease expired a minute ago
        conn.execute(
            "UPDATE runs SET lease_expires_at = now() - interval '60 seconds' WHERE id = %s",
            (run_id,),
        )

    assert run_id in reap_exhausted("pytest-reaper")
    run = run_row(run_id)
    assert run["status"] == "dead"
    assert run["error"] == {"reason": "max_attempts_exhausted"}
    assert step_kinds(run_id)[-1] == "error"  # the marker row
    assert reap_exhausted("pytest-reaper") == []  # reaping is one-shot per run


def test_reaper_leaves_live_and_unexhausted_runs_alone(reparked: uuid.UUID) -> None:
    run_id = reparked
    from relay.db import pool

    # attempts left -> not reaped, even with an expired lease
    set_run(run_id, status="running", attempt_count=2, max_attempts=5, lease_owner="w")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET lease_expires_at = now() - interval '60 seconds' WHERE id = %s",
            (run_id,),
        )
    assert run_id not in reap_exhausted("pytest-reaper")

    # exhausted but the lease is still live (the owner is mid-final-attempt) -> not reaped
    set_run(run_id, attempt_count=5)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET lease_expires_at = now() + interval '60 seconds' WHERE id = %s",
            (run_id,),
        )
    assert run_id not in reap_exhausted("pytest-reaper")
    assert run_row(run_id)["status"] == "running"


def test_requeue_appends_the_marker_then_flips(reparked: uuid.UUID) -> None:
    run_id = reparked
    from relay.db import pool

    with pool.connection() as conn:  # the failed run's error step, as the loop writes it
        conn.execute(
            "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
            "VALUES (%s, 1, 'error', %s, 'pytest')",
            (run_id, Jsonb({"reason": "model_unavailable", "detail": "503"})),
        )
    set_run(run_id, status="failed", attempt_count=1, error=Jsonb({"reason": "x"}))

    assert requeue_run(run_id, "pytest-operator") == "requeued"
    run = run_row(run_id)
    assert run["status"] == "pending"
    assert run["lease_owner"] is None and run["error"] is None
    assert run["max_attempts"] == 1 + REQUEUE_EXTRA_ATTEMPTS
    assert step_kinds(run_id) == ["run_started", "error", "requeued"]


def test_requeue_refuses_a_run_that_is_not_parked(reparked: uuid.UUID) -> None:
    run_id = reparked
    set_run(run_id, status="running")
    outcome = requeue_run(run_id, "pytest-operator")
    assert outcome.startswith("not requeued")
    assert run_row(run_id)["status"] == "running"
    assert "requeued" not in step_kinds(run_id)  # no marker was written either


def test_requeue_can_raise_the_budget(reparked: uuid.UUID) -> None:
    run_id = reparked
    set_run(run_id, status="failed")
    assert requeue_run(run_id, "pytest-operator", new_budget_usd=0.75) == "requeued"
    assert float(run_row(run_id)["budget_usd"]) == 0.75


def test_requeue_of_a_missing_run(db: None) -> None:
    assert requeue_run(uuid.uuid4(), "pytest-operator") == "not found"
