"""The client Idempotency-Key on POST /v1/runs, against the real app and database.

The first of the three idempotency layers: a retried POST must not create a
second run. Runs made here are parked as 'dead' afterwards so no live worker
claims them; each test gets its own tenant so the caps and keys never collide.
"""

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import relay.api.principal as principal_mod
import relay.api.ratelimit as ratelimit_mod
from relay.api.main import app
from scripts.seed import create_api_key


@pytest.fixture
def tenant(db: None) -> Iterator[dict[str, Any]]:
    """A fresh tenant (cap 3) with one API key; its runs are parked afterwards."""
    from relay.db import pool

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (name, max_concurrent_runs, requests_per_minute) "
                "VALUES (%s, 3, 1000) RETURNING id",
                (f"pytest-idem-{uuid.uuid4().hex[:6]}",),
            )
            row = cur.fetchone()
            assert row is not None
            key = create_api_key(cur, row[0])
    principal_mod._cache.clear()
    ratelimit_mod._windows.clear()
    yield {"id": row[0], "headers": {"Authorization": f"Bearer {key}"}}
    with pool.connection() as conn:
        conn.execute("UPDATE runs SET status = 'dead' WHERE tenant_id = %s", (row[0],))


def runs_of(tenant_id: uuid.UUID) -> int:
    from relay.db import pool

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM runs WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
    return 0 if row is None else int(row[0])


def post(client: TestClient, headers: dict[str, str], domain: str, key: str | None = None) -> Any:
    extra = {} if key is None else {"Idempotency-Key": key}
    return client.post("/v1/runs", json={"domain": domain}, headers={**headers, **extra})


def test_same_key_same_body_returns_the_same_run(tenant: dict[str, Any]) -> None:
    client = TestClient(app)
    first = post(client, tenant["headers"], "idem.example", "key-1")
    again = post(client, tenant["headers"], "idem.example", "key-1")
    assert first.status_code == again.status_code == 202
    assert first.json()["run_id"] == again.json()["run_id"]
    assert "Idempotent-Replayed" not in first.headers
    assert again.headers["Idempotent-Replayed"] == "true"
    assert runs_of(tenant["id"]) == 1


def test_same_key_different_body_is_422(tenant: dict[str, Any]) -> None:
    client = TestClient(app)
    assert post(client, tenant["headers"], "first.example", "key-2").status_code == 202
    clash = post(client, tenant["headers"], "second.example", "key-2")
    assert clash.status_code == 422
    assert runs_of(tenant["id"]) == 1  # the clashing request created nothing


def test_no_key_keeps_the_old_behaviour(tenant: dict[str, Any]) -> None:
    client = TestClient(app)
    a = post(client, tenant["headers"], "plain.example")
    b = post(client, tenant["headers"], "plain.example")
    assert a.json()["run_id"] != b.json()["run_id"]
    assert runs_of(tenant["id"]) == 2


def test_keys_are_per_tenant(tenant: dict[str, Any]) -> None:
    from relay.db import pool

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
                (f"pytest-idem-other-{uuid.uuid4().hex[:6]}",),
            )
            row = cur.fetchone()
            assert row is not None
            other = {"Authorization": f"Bearer {create_api_key(cur, row[0])}"}
    client = TestClient(app)
    mine = post(client, tenant["headers"], "shared.example", "same-string")
    theirs = post(client, other, "shared.example", "same-string")
    assert mine.json()["run_id"] != theirs.json()["run_id"]  # no cross-tenant replay
    with pool.connection() as conn:
        conn.execute("UPDATE runs SET status = 'dead' WHERE tenant_id = %s", (row[0],))


def test_replay_is_not_refused_by_the_concurrency_cap(tenant: dict[str, Any]) -> None:
    client = TestClient(app)
    first = post(client, tenant["headers"], "cap0.example", "key-cap")
    for i in (1, 2):  # fill the cap of 3
        assert post(client, tenant["headers"], f"cap{i}.example").status_code == 202
    assert post(client, tenant["headers"], "cap3.example").status_code == 429  # new work: refused
    replay = post(client, tenant["headers"], "cap0.example", "key-cap")  # creates nothing
    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]


def test_malformed_keys_are_400(tenant: dict[str, Any]) -> None:
    client = TestClient(app)
    # raw bytes: the HTTP client refuses to encode non-ASCII strings itself
    for bad in (b"x" * 256, b"tab\there", "ümlaut".encode()):
        r = client.post(
            "/v1/runs",
            json={"domain": "bad.example"},
            headers={**tenant["headers"], "Idempotency-Key": bad},
        )
        assert r.status_code == 400, bad
    assert runs_of(tenant["id"]) == 0


def test_eight_racing_retries_create_one_run(tenant: dict[str, Any]) -> None:
    """A client that retries aggressively: 8 parallel POSTs, same key, same body."""
    client = TestClient(app)
    barrier = threading.Barrier(8)
    ids: list[str] = []
    codes: list[int] = []

    def fire() -> None:
        barrier.wait()
        r = post(client, tenant["headers"], "race.example", "key-race")
        codes.append(r.status_code)
        ids.append(r.json()["run_id"])

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert codes == [202] * 8
    assert len(set(ids)) == 1  # everyone got the same run
    assert runs_of(tenant["id"]) == 1
