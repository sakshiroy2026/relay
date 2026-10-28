"""The dead-letter queue, from the operator's chair.

Usage:
  python -m scripts.dlq list [--status failed,dead] [--limit 50]
  python -m scripts.dlq show <run_id>
  python -m scripts.dlq requeue <run_id> [<run_id> ...] [--budget 0.5]
  python -m scripts.dlq requeue --reason model_unavailable [--limit 50]

`list` groups what's parked and why (the reason is the last error /
budget_exceeded step, or runs.error for reaped runs). `requeue` puts runs back
in the queue via relay.core.dlq — the run resumes from its ledger, it does not
restart. Requeueing an invalid_final_answer run without changing anything will
usually just fail again (the repair ladder already spent its re-ask); the tool
warns rather than refuses, because the operator may have fixed the cause.
"""

import argparse
import sys
import uuid
from typing import Any

from psycopg.rows import dict_row

from relay.core.dlq import requeue_run
from relay.db import pool

LIST_SQL = """
SELECT r.id, r.status::text AS status, r.attempt_count, r.max_attempts,
       r.created_at, r.input->>'domain' AS domain,
       COALESCE(
           (SELECT s.payload->>'reason' FROM steps s
            WHERE s.run_id = r.id AND s.kind IN ('error', 'budget_exceeded')
            ORDER BY s.step_index DESC LIMIT 1),
           r.error->>'reason',
           'unknown'
       ) AS reason
FROM runs r
WHERE r.status::text = ANY(%s)
ORDER BY r.created_at DESC
LIMIT %s
"""


def list_parked(statuses: list[str], limit: int) -> list[dict[str, Any]]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return [dict(r) for r in cur.execute(LIST_SQL, (statuses, limit)).fetchall()]


def cmd_list(args: argparse.Namespace) -> int:
    rows = list_parked(args.status.split(","), args.limit)
    if not rows:
        print("nothing parked")
        return 0
    by_reason: dict[str, int] = {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    print(f"{len(rows)} run(s):  " + "  ".join(f"{k}×{v}" for k, v in sorted(by_reason.items())))
    for r in rows:
        print(
            f"  {str(r['id'])[:8]}  {r['status']:<6} attempts {r['attempt_count']}/"
            f"{r['max_attempts']}  {r['reason']:<24} {r['domain']}  {r['created_at']:%m-%d %H:%M}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        steps = cur.execute(
            "SELECT step_index, kind, written_by, payload FROM steps WHERE run_id = %s "
            "ORDER BY step_index",
            (args.run_id,),
        ).fetchall()
    for s in steps:
        line = str(s["payload"])
        print(f"  {s['step_index']:>3} {s['kind']:<17} {s['written_by']:<20} {line[:90]}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    if args.run_ids:
        targets = [uuid.UUID(r) for r in args.run_ids]
    elif args.reason:
        rows = list_parked(["failed", "dead"], args.limit)
        targets = [r["id"] for r in rows if r["reason"] == args.reason]
        if not targets:
            print(f"no failed/dead runs with reason {args.reason!r}")
            return 0
    else:
        print("requeue needs run ids or --reason")
        return 2

    failures = 0
    for run_id in targets:
        outcome = requeue_run(run_id, requeued_by="scripts.dlq", new_budget_usd=args.budget)
        print(f"  {str(run_id)[:8]}  {outcome}")
        failures += 0 if outcome.startswith("requeued") else 1
        if outcome.startswith("requeued") and args.reason == "invalid_final_answer":
            print("           note: without a change, the same answer will likely fail again")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default="failed,dead")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show")
    p_show.add_argument("run_id")

    p_req = sub.add_parser("requeue")
    p_req.add_argument("run_ids", nargs="*")
    p_req.add_argument("--reason")
    p_req.add_argument("--limit", type=int, default=50)
    p_req.add_argument("--budget", type=float, default=None, help="also raise the run's budget_usd")

    args = parser.parse_args()
    try:
        code = {"list": cmd_list, "show": cmd_show, "requeue": cmd_requeue}[args.cmd](args)
    finally:
        pool.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
