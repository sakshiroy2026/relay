"""Day 10 against the real app: full keys, tenant isolation, both 429s.

Runs created here are parked as 'dead' on teardown so no live worker ever
claims them. bcrypt is slow on purpose, so key churn is kept low: two tenants,
one key each, made once per module.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import relay.api.principal as principal_mod
import relay.api.ratelimit as ratelimit_mod
from relay.api.main import app
from relay.api.principal import Principal
from scripts.seed import create_api_key


@pytest.fixture(scope="module")
def two_tenants(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    from tests.conftest import _db_available

    if not _db_available():
        pytest.skip("no database reachable")
    from relay.db import pool

    made: dict[str, Any] = {"tenants": [], "run_ids": []}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for name in ("pytest-a", "pytest-b"):
                cur.execute(
                    "INSERT INTO tenants (name, max_concurrent_runs, requests_per_minute) "
                    "VALUES (%s, 3, 1000) RETURNING id",
                    (f"{name}-{uuid.uuid4().hex[:6]}",),
                )
                row = cur.fetchone()
                assert row is not None
                made["tenants"].append({"id": row[0], "key": create_api_key(cur, row[0])})
    yield made
    with pool.connection() as conn:  # park every run this module created
        conn.execute(
            "UPDATE runs SET status = 'dead' WHERE tenant_id = ANY(%s) "
            "AND status IN ('pending', 'running')",
            ([t["id"] for t in made["tenants"]],),
        )


@pytest.fixture
def client(two_tenants: dict[str, Any]) -> Iterator[TestClient]:
    principal_mod._cache.clear()
    ratelimit_mod._windows.clear()
    with TestClient(app) as c:
        yield c


def auth(two_tenants: dict[str, Any], i: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {two_tenants['tenants'][i]['key']}"}


def test_no_key_and_wrong_scheme_are_401(client: TestClient) -> None:
    assert client.get("/v1/runs/" + str(uuid.uuid4())).status_code == 401
    assert (
        client.get(
            "/v1/runs/" + str(uuid.uuid4()), headers={"Authorization": "Basic abc"}
        ).status_code
        == 401
    )
    assert client.get("/healthz").status_code == 200  # stays open


def test_wrong_secret_same_prefix_is_401(client: TestClient, two_tenants: dict[str, Any]) -> None:
    prefix = two_tenants["tenants"][0]["key"].split(".")[0]
    r = client.get(
        "/v1/runs/" + str(uuid.uuid4()),
        headers={"Authorization": f"Bearer {prefix}.wrong-secret"},
    )
    assert r.status_code == 401


def test_revoked_key_is_401_after_the_cache_expires(
    client: TestClient, two_tenants: dict[str, Any]
) -> None:
    from relay.db import pool

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
                (f"pytest-revoked-{uuid.uuid4().hex[:6]}",),
            )
            row = cur.fetchone()
            assert row is not None
            key = create_api_key(cur, row[0])
    r = client.get("/v1/runs/" + str(uuid.uuid4()), headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 404  # authenticated; the run just doesn't exist

    with pool.connection() as conn:
        conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE key_prefix = %s", (key.split(".")[0],)
        )
    principal_mod._cache.clear()  # the documented trade-off: revocation bites at cache expiry
    r = client.get("/v1/runs/" + str(uuid.uuid4()), headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


def test_a_tenants_run_is_invisible_to_the_other(
    client: TestClient, two_tenants: dict[str, Any]
) -> None:
    created = client.post(
        "/v1/runs", json={"domain": "tenant-a.example"}, headers=auth(two_tenants, 0)
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    assert client.get(f"/v1/runs/{run_id}", headers=auth(two_tenants, 0)).status_code == 200
    assert client.get(f"/v1/runs/{run_id}", headers=auth(two_tenants, 1)).status_code == 404
    assert client.get(f"/v1/runs/{run_id}/steps", headers=auth(two_tenants, 1)).json() == []


def test_concurrency_cap_returns_429_with_retry_after(
    client: TestClient, two_tenants: dict[str, Any]
) -> None:
    headers = auth(two_tenants, 1)  # cap is 3 (the fixture made it so)
    codes = [
        client.post("/v1/runs", json={"domain": f"cap-{i}.example"}, headers=headers).status_code
        for i in range(4)
    ]
    assert codes == [202, 202, 202, 429]
    fourth = client.post("/v1/runs", json={"domain": "cap-4.example"}, headers=headers)
    assert "too many open runs" in fourth.json()["detail"]
    assert "Retry-After" in fourth.headers
    # the other tenant is not affected by this tenant's cap
    ok = client.post("/v1/runs", json={"domain": "other.example"}, headers=auth(two_tenants, 0))
    assert ok.status_code == 202


def test_rate_limit_window(two_tenants: dict[str, Any]) -> None:
    ratelimit_mod._windows.clear()
    p = Principal(
        tenant_id=uuid.uuid4(), key_name="t", max_concurrent_runs=5, requests_per_minute=3
    )
    clock = [1000.0]
    for _ in range(3):
        ratelimit_mod.check(p, now=lambda: clock[0])
    with pytest.raises(Exception) as exc:
        ratelimit_mod.check(p, now=lambda: clock[0])
    assert exc.value.status_code == 429  # type: ignore[attr-defined]
    assert exc.value.headers["Retry-After"]  # type: ignore[attr-defined]

    clock[0] += 60  # the next window opens
    ratelimit_mod.check(p, now=lambda: clock[0])


def test_racing_creates_cannot_squeeze_past_the_cap(
    client: TestClient, two_tenants: dict[str, Any]
) -> None:
    """8 parallel creates, cap 3 (fresh tenant): exactly 3 may win the advisory lock race."""
    import threading

    from relay.db import pool

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (name, max_concurrent_runs, requests_per_minute) "
                "VALUES (%s, 3, 1000) RETURNING id",
                (f"pytest-race-{uuid.uuid4().hex[:6]}",),
            )
            row = cur.fetchone()
            assert row is not None
            tenant_id, key = row[0], create_api_key(cur, row[0])

    barrier = threading.Barrier(8)
    codes: list[int] = []

    def post(i: int) -> None:
        barrier.wait()
        r = client.post(
            "/v1/runs",
            json={"domain": f"race-{i}.example"},
            headers={"Authorization": f"Bearer {key}"},
        )
        codes.append(r.status_code)

    threads = [threading.Thread(target=post, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(codes) == [202, 202, 202, 429, 429, 429, 429, 429]
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) FROM runs WHERE tenant_id = %s", (tenant_id,)).fetchone()
        conn.execute("UPDATE runs SET status = 'dead' WHERE tenant_id = %s", (tenant_id,))
    assert n is not None and int(n[0]) == 3  # the database agrees with the status codes
