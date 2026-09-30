"""Copy a run's spend and token totals from the ledger onto its runs row.

WHAT IT IS  A display helper. The ledger's model_call rows are the truth
            about spend; this writes their sums to runs.spent_usd /
            tokens_in / tokens_out so GET /v1/runs/{id} can show them.
GOES IN     run_id.
COMES OUT   Nothing.
TOUCHES     One UPDATE of runs, computed from steps in the same statement.
FAILS WHEN  Database errors propagate. Running it twice, late, or from a stale
            worker is harmless: it always writes the ledger's current sums.
"""

from uuid import UUID

from relay.db import pool


def refresh_run_totals(run_id: UUID) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE runs SET
                spent_usd  = t.spent,
                tokens_in  = t.tokens_in,
                tokens_out = t.tokens_out
            FROM (
                SELECT COALESCE(sum((payload->>'cost_usd')::numeric), 0)      AS spent,
                       COALESCE(sum((payload->>'input_tokens')::bigint), 0)   AS tokens_in,
                       COALESCE(sum((payload->>'output_tokens')::bigint), 0)  AS tokens_out
                FROM steps
                WHERE run_id = %(run_id)s AND kind = 'model_call'
            ) AS t
            WHERE runs.id = %(run_id)s
            """,
            {"run_id": run_id},
        )
