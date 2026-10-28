"""P1: a tenant's run_mode decides which model its runs use, and live runs are budgeted tight.

'demo' (every tenant by default) = the free fake model; 'live' = the real model,
switched on per tenant on purpose. The mode is copied into runs.input when the
run is created, so it can't change under a run in flight.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import relay.api.principal as principal_mod
import relay.api.ratelimit as ratelimit_mod
from relay.api.main import app
from relay.config import settings
from relay.llm.factory import resolve_mode
from scripts.seed import create_api_key


@pytest.fixture
def tenant(db: None, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    from relay.db import pool

    mode = request.param
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (name, run_mode) VALUES (%s, %s) RETURNING id",
                (f"pytest-mode-{uuid.uuid4().hex[:6]}", mode),
            )
            row = cur.fetchone()
            assert row is not None
            key = create_api_key(cur, row[0])
    principal_mod._cache.clear()
    ratelimit_mod._windows.clear()
    yield {"id": row[0], "headers": {"Authorization": f"Bearer {key}"}, "mode": mode}
    with pool.connection() as conn:  # park: no live worker may pick these up
        conn.execute("UPDATE runs SET status = 'dead' WHERE tenant_id = %s", (row[0],))


def created_run(tenant: dict[str, Any]) -> dict[str, Any]:
    from psycopg.rows import dict_row

    from relay.db import pool

    r = TestClient(app).post("/v1/runs", json={"domain": "m.example"}, headers=tenant["headers"])
    assert r.status_code == 202
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT input, budget_usd FROM runs WHERE id = %s", (r.json()["run_id"],)
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize("tenant", ["demo"], indirect=True)
def test_demo_tenant_runs_are_free_and_fake(tenant: dict[str, Any]) -> None:
    run = created_run(tenant)
    assert run["input"]["mode"] == "demo"
    assert resolve_mode(run["input"]["mode"]) == ("fake", "fake")
    assert float(run["budget_usd"]) == 0.25  # the column default (fake prices)


@pytest.mark.parametrize("tenant", ["live"], indirect=True)
def test_live_tenant_runs_use_the_real_model_with_a_tight_budget(
    tenant: dict[str, Any],
) -> None:
    run = created_run(tenant)
    assert run["input"]["mode"] == "live"
    assert resolve_mode(run["input"]["mode"]) == ("anthropic", "real")
    assert float(run["budget_usd"]) == settings.live_run_budget_usd


def test_new_tenants_default_to_demo(db: None) -> None:
    from relay.db import pool

    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING run_mode",
            (f"pytest-default-{uuid.uuid4().hex[:6]}",),
        ).fetchone()
    assert row is not None and row[0] == "demo"  # paying is opt-in, never the default


def test_unknown_modes_are_refused() -> None:
    with pytest.raises(RuntimeError, match="unknown run mode"):
        resolve_mode("free-for-all")
    assert resolve_mode(None) == (settings.llm_provider, settings.tools_provider)
