"""The demo "Kill the worker" button: POST /v1/runs/{id}/crash + the worker's check.

WHAT IT IS  The crash flag end to end, minus the actual death: the API may set
            it only for a demo tenant's RUNNING run, and the worker's
            heartbeat check clears it (one-shot) and exits. os._exit is
            monkeypatched — the test process must survive.
GOES IN     A reachable migrated database (the `db` fixture skips otherwise).
COMES OUT   Assertions; rows are parked with unexpired leases so no live
            worker claims them mid-test.
TOUCHES     tenants, api_keys, runs.
FAILS WHEN  The endpoint lets a live tenant kill workers, the flag survives a
            crash (the next attempt would die too), or a non-owner clears it.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

import relay.api.principal as principal_mod
import relay.worker as worker_mod
from relay.api.main import app
from relay.db import pool
from scripts.seed import create_api_key

OWNER = "pytest-worker:1"


@pytest.fixture
def key_for(db: None) -> Iterator[Any]:
    def make(mode: str) -> tuple[uuid.UUID, str]:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (name, run_mode) VALUES (%s, %s) RETURNING id",
                    (f"pytest-crash-{uuid.uuid4().hex[:6]}", mode),
                )
                row = cur.fetchone()
                assert row is not None
                return row[0], create_api_key(cur, row[0])

    principal_mod._cache.clear()
    yield make


def make_run(tenant_id: uuid.UUID, status: str, owner: str | None = OWNER) -> uuid.UUID:
    """A run no live worker can claim: 'running' with a fresh (unexpired) lease."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO runs (tenant_id, input, agent_version, status, lease_owner, "
            "lease_expires_at) VALUES (%s, %s, 'pytest', %s, %s, "
            "CASE WHEN %s::text IS NULL THEN NULL ELSE now() + interval '5 minutes' END) "
            "RETURNING id",
            (tenant_id, Jsonb({"domain": "crash.example"}), status, owner, owner),
        ).fetchone()
        assert row is not None
        return row[0]


def flag_at(run_id: uuid.UUID) -> Any:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT crash_requested_at FROM runs WHERE id = %s", (run_id,)
        ).fetchone()
        assert row is not None
        return row[0]


def crash(key: str, run_id: uuid.UUID) -> Any:
    return TestClient(app).post(
        f"/v1/runs/{run_id}/crash", headers={"Authorization": f"Bearer {key}"}
    )


# ── the endpoint ───────────────────────────────────────────────────────────


def test_demo_tenant_sets_the_flag_on_a_running_run(key_for: Any) -> None:
    tenant_id, key = key_for("demo")
    run_id = make_run(tenant_id, "running")
    res = crash(key, run_id)
    assert res.status_code == 202 and flag_at(run_id) is not None


def test_a_run_without_a_live_owner_is_409(key_for: Any) -> None:
    tenant_id, key = key_for("demo")
    assert crash(key, make_run(tenant_id, "pending", owner=None)).status_code == 409
    assert crash(key, make_run(tenant_id, "succeeded")).status_code == 409


def test_a_live_tenant_is_refused(key_for: Any) -> None:
    tenant_id, key = key_for("live")
    run_id = make_run(tenant_id, "running")
    assert crash(key, run_id).status_code == 403 and flag_at(run_id) is None


def test_another_tenants_run_is_404(key_for: Any) -> None:
    other_tenant, _ = key_for("demo")
    _, key = key_for("demo")
    assert crash(key, make_run(other_tenant, "running")).status_code == 404
    assert crash(key, uuid.uuid4()).status_code == 404


# ── the worker's check ─────────────────────────────────────────────────────


def exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    codes: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: codes.append(code))
    monkeypatch.setattr(worker_mod, "WORKER_ID", OWNER)
    return codes


def test_the_owner_clears_the_flag_and_exits_once(
    key_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, key = key_for("demo")
    run_id = make_run(tenant_id, "running")
    crash(key, run_id)
    codes = exits(monkeypatch)
    worker_mod.crash_if_requested(run_id)
    assert codes == [137] and flag_at(run_id) is None  # one-shot: flag gone before the exit
    worker_mod.crash_if_requested(run_id)  # the resumed attempt must survive
    assert codes == [137]


def test_without_the_flag_nothing_happens(key_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, _ = key_for("demo")
    run_id = make_run(tenant_id, "running")
    codes = exits(monkeypatch)
    worker_mod.crash_if_requested(run_id)
    assert codes == [] and flag_at(run_id) is None


def test_a_non_owner_does_not_clear_someone_elses_flag(
    key_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, key = key_for("demo")
    run_id = make_run(tenant_id, "running")  # owned by OWNER
    crash(key, run_id)
    codes: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: codes.append(code))
    monkeypatch.setattr(worker_mod, "WORKER_ID", "someone-else:2")
    worker_mod.crash_if_requested(run_id)
    assert codes == [] and flag_at(run_id) is not None  # still armed for the real owner
