"""Deployment: /demo-config offers a public key only if its tenant runs the free model."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import relay.api.main as main_mod
import relay.api.principal as principal_mod
from relay.api.main import app
from scripts.seed import create_api_key


@pytest.fixture
def key_for(db: None) -> Iterator[object]:
    from relay.db import pool

    made: list[uuid.UUID] = []

    def make(mode: str) -> str:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (name, run_mode) VALUES (%s, %s) RETURNING id",
                    (f"pytest-demo-cfg-{uuid.uuid4().hex[:6]}", mode),
                )
                row = cur.fetchone()
                assert row is not None
                made.append(row[0])
                return create_api_key(cur, row[0])

    principal_mod._cache.clear()
    yield make


def test_unset_offers_nothing(monkeypatch: pytest.MonkeyPatch, db: None) -> None:
    monkeypatch.setattr(main_mod.settings, "public_demo_key", None)
    body = TestClient(app).get("/demo-config").json()
    assert body["demo_key"] is None and body["real_ai_runs_left"] == 0


def test_a_demo_tenant_key_is_offered(monkeypatch: pytest.MonkeyPatch, key_for) -> None:
    key = key_for("demo")
    monkeypatch.setattr(main_mod.settings, "public_demo_key", key)
    body = TestClient(app).get("/demo-config").json()
    assert body["demo_key"] == key and "free" in body["note"]


def test_a_live_tenant_key_is_never_offered(monkeypatch: pytest.MonkeyPatch, key_for) -> None:
    monkeypatch.setattr(main_mod.settings, "public_demo_key", key_for("live"))
    body = TestClient(app).get("/demo-config").json()
    assert body["demo_key"] is None and body["real_ai_runs_left"] == 0


def test_a_wrong_key_is_not_offered(monkeypatch: pytest.MonkeyPatch, db: None) -> None:
    monkeypatch.setattr(main_mod.settings, "public_demo_key", "rly_nope.nope")
    body = TestClient(app).get("/demo-config").json()
    assert body["demo_key"] is None and body["real_ai_runs_left"] == 0


def test_a_saved_run_link_waits_for_the_config() -> None:
    # A first-time visitor's key arrives with /demo-config: streaming the run in
    # the URL before that answers 401 and shows "API key missing".
    page = TestClient(app).get("/").text
    assert "const configReady = loadConfig();" in page
    saved_link = page[page.index("location.hash.match") :]
    assert saved_link.index("configReady.then") < saved_link.index("stream()")
