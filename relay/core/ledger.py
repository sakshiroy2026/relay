"""The ledger: append-only writes to the steps table."""

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from relay.db import pool


class LeaseLost(Exception):
    """This worker no longer owns the run. Stop and touch nothing else."""


def next_step_index(run_id: UUID) -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(step_index), -1) + 1 FROM steps WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()

    if row is None:  # can't happen: MAX() with no GROUP BY always returns one row
        raise RuntimeError("aggregate query returned no row")
    return int(row[0])


def append_step(
    run_id: UUID,
    step_index: int,
    kind: str,
    payload: dict[str, Any],
    written_by: str,
) -> int:
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO steps (run_id, step_index, kind, payload, written_by)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (run_id, step_index, kind, Jsonb(payload), written_by),
                )
    except psycopg.errors.UniqueViolation as exc:
        raise LeaseLost(f"step {step_index} of run {run_id} already written") from exc

    return step_index + 1
