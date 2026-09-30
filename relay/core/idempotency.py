"""Tool-level idempotency: a write tool's effect commits at most once per key.

WHAT IT IS  The receipt book for write tools. Before a write runs, its key is
            written down. If the key already has a completed result, that result
            is handed back and the write does NOT run again.
GOES IN     idem_key ('<run_id>:<tool_call_id>': the same on every re-run of the
            same model request), run_id, the tool_call row's index, the tool
            name, and `execute(cur) -> dict`: the write, done on the cursor it
            is given.
COMES OUT   The tool's result dict: freshly computed, or the stored one.
TOUCHES     tool_invocations (always); whatever `execute` writes (companies).
            Two transactions: (1) record the key, (2) the write + marking the
            key 'ok', which commit TOGETHER or not at all.
FAILS WHEN  Database errors propagate (a code bug crashes, a network blip is
            retried by the worker). A second worker racing on the same key
            doesn't fail: its write is rolled back and it gets the stored result.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relay.db import pool

Execute = Callable[[psycopg.Cursor[Any]], dict[str, Any]]


def run_once(
    idem_key: str,
    run_id: UUID,
    step_index: int,
    tool_name: str,
    execute: Execute,
) -> dict[str, Any]:
    # ── 1. insert BEFORE execute (commits on its own) ─────────────────────
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO tool_invocations (idem_key, run_id, step_index, tool_name, status)
                VALUES (%s, %s, %s, %s, 'in_flight')
                ON CONFLICT (idem_key)
                    DO UPDATE SET attempts = tool_invocations.attempts + 1
                RETURNING status, result
                """,
                (idem_key, run_id, step_index, tool_name),
            )
            seen = cur.fetchone()

    if seen is not None and seen["status"] == "ok":
        return dict(seen["result"])  # done before: hand back the receipt, don't redo

    # ── 2. the write and "mark ok" in ONE transaction ─────────────────────
    with pool.connection() as conn:
        with conn.cursor() as cur:
            result = execute(cur)
            cur.execute(
                """
                UPDATE tool_invocations
                SET status = 'ok', result = %s, completed_at = now(),
                    executions = executions + 1
                WHERE idem_key = %s AND status = 'in_flight'
                """,
                (Jsonb(result), idem_key),
            )
            if cur.rowcount == 1:
                return result  # leaving the block commits the write and the receipt

            # Someone else completed this key while we were writing:
            # undo our write and return theirs.
            conn.rollback()
            cur.execute("SELECT result FROM tool_invocations WHERE idem_key = %s", (idem_key,))
            row = cur.fetchone()

    if row is None:
        raise RuntimeError(f"tool_invocations row {idem_key} vanished")
    return dict(row[0])
