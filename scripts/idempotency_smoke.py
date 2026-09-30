"""Day 7 smoke test: save_company through dispatch_tool, called the ways a crash would call it.

Usage: python -m scripts.idempotency_smoke

Creates one parked run (status 'dead', so no worker ever claims it) with a unique
domain, then:
  1. first call with key K1          -> executes, company created
  2. same key K1 again (a re-run)    -> stored result, NOT executed again
  3. new key K2, same domain         -> executes, UPDATE of the same row (UNIQUE wins)
  4. key K3 left 'in_flight' (worker died before the write committed) -> executes once
  5. lookup_existing finds the row
Prints PASS/FAIL. Leaves its rows behind (test data, like drain_test).
"""

import json
import sys
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from relay.agent.fake_script import ENRICH_SCRIPT
from relay.agent.tools import ToolContext, dispatch_tool
from relay.config import settings
from relay.db import pool

RECORD = ENRICH_SCRIPT[2].tool_calls[0].args  # the fake model's save_company args


def make_parked_run(domain: str) -> uuid.UUID:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO runs (tenant_id, input, agent_version, status)
            VALUES (%s, %s, 'idem-smoke', 'dead')
            RETURNING id
            """,
            (settings.dev_tenant_id, Jsonb({"domain": domain})),
        ).fetchone()
    if row is None:
        raise RuntimeError("run insert returned no row")
    return row[0]


def invocation(key: str) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, attempts, executions FROM tool_invocations WHERE idem_key = %s",
            (key,),
        ).fetchone()
    return {} if row is None else {"status": row[0], "attempts": row[1], "executions": row[2]}


def company_count(domain: str) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM companies WHERE domain = %s", (domain,)).fetchone()
    return 0 if row is None else int(row[0])


def save(run_id: uuid.UUID, domain: str, call_id: str) -> dict[str, Any]:
    ctx = ToolContext(run_id=run_id, domain=domain, step_index=10, idem_key=f"{run_id}:{call_id}")
    result = dispatch_tool("save_company", RECORD, ctx)
    assert result.ok, result.content
    return dict(json.loads(result.content))


def main() -> None:
    domain = f"smoke-{uuid.uuid4().hex[:8]}.example"
    run_id = make_parked_run(domain)
    print(f"run {run_id}  domain {domain}")
    checks: list[tuple[str, bool]] = []

    r1 = save(run_id, domain, "K1")
    inv = invocation(f"{run_id}:K1")
    print("1. first call      ", r1, inv)
    checks.append(("1 created", r1["created"] is True and inv["executions"] == 1))

    r2 = save(run_id, domain, "K1")
    inv = invocation(f"{run_id}:K1")
    print("2. same key again  ", r2, inv)
    checks.append(("2 stored result", r2 == r1))
    checks.append(("2 not re-executed", inv["attempts"] == 2 and inv["executions"] == 1))
    checks.append(("2 one company", company_count(domain) == 1))

    r3 = save(run_id, domain, "K2")
    print("3. new key         ", r3, invocation(f"{run_id}:K2"))
    checks.append(
        ("3 same row updated", r3["created"] is False and r3["company_id"] == r1["company_id"])
    )
    checks.append(("3 still one company", company_count(domain) == 1))

    with pool.connection() as conn:  # a worker that died between step 1 and step 2
        conn.execute(
            "INSERT INTO tool_invocations (idem_key, run_id, step_index, tool_name, status) "
            "VALUES (%s, %s, 10, 'save_company', 'in_flight')",
            (f"{run_id}:K3", run_id),
        )
    save(run_id, domain, "K3")
    inv = invocation(f"{run_id}:K3")
    print("4. in_flight retry ", inv)
    checks.append(("4 executed once", inv == {"status": "ok", "attempts": 2, "executions": 1}))

    ctx = ToolContext(run_id=run_id, domain=domain, step_index=11, idem_key=f"{run_id}:L1")
    found = json.loads(dispatch_tool("lookup_existing", {"domain": domain}, ctx).content)
    print("5. lookup_existing ", {k: found[k] for k in ("found", "name")})
    checks.append(("5 found", found["found"] is True))

    pool.close()
    print()
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    passed = all(ok for _, ok in checks)
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
