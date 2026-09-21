"""Print a run's status and ledger. Usage: python -m scripts.show_steps <run_id>"""

import sys

from psycopg.rows import dict_row

from relay.db import pool


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.show_steps <run_id>")
        return
    run_id = sys.argv[1]

    try:
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT status, attempt_count, lease_owner FROM runs WHERE id = %s",
                    (run_id,),
                )
                run = cur.fetchone()
                cur.execute(
                    "SELECT step_index, kind, written_by, created_at FROM steps "
                    "WHERE run_id = %s ORDER BY step_index",
                    (run_id,),
                )
                steps = cur.fetchall()
    finally:
        pool.close()

    if run is None:
        print("no such run")
        return

    print(f"status={run['status']}  attempts={run['attempt_count']}  owner={run['lease_owner']}")
    for s in steps:
        print(
            f"  {s['step_index']:>3}  {s['kind']:<12} {s['written_by']:<26} "
            f"{s['created_at']:%H:%M:%S}"
        )

    indexes = [s["step_index"] for s in steps]
    no_gaps = indexes == list(range(len(indexes)))
    print(f"{len(indexes)} steps, numbered 0..{len(indexes) - 1} with no gaps: {no_gaps}")


if __name__ == "__main__":
    main()
