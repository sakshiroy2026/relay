"""Hand test for relay/core/ledger.py: write a step, then try the same index again."""

from relay.core.ledger import LeaseLost, append_step, next_step_index
from relay.db import pool


def main() -> None:
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            print("no runs in the database")
            return
        run_id = row[0]
        print(f"run: {run_id}")

        n = next_step_index(run_id)
        print(f"worker A believes next index is {n}")

        after = append_step(run_id, n, "model_call", {"test": "first"}, "manual-test")
        print(f"wrote step {n}; next would be {after}")

        try:
            append_step(run_id, n, "model_call", {"test": "second"}, "manual-test")
            print("FAIL: the duplicate step was accepted")
        except LeaseLost as exc:
            print(f"OK, fenced: {exc}")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
