"""Day 17: Prometheus metrics from the ledger, and trace ids on ledger rows."""

import uuid

import pytest
from fastapi.testclient import TestClient

from relay.core.ledger import append_step
from relay.obs.tracing import current_trace_id, setup, tracer


@pytest.fixture
def client(db: None) -> TestClient:
    from relay.api.main import app

    return TestClient(app)


def test_metrics_endpoint_is_open_and_serves_relay_families(client: TestClient) -> None:
    r = client.get("/metrics")  # no API key: counts only, open like /healthz
    assert r.status_code == 200
    body = r.text
    for family in (
        "relay_runs",
        "relay_steps_total",
        "relay_spend_usd",
        "relay_resumed_runs",
        "relay_dlq_depth",
        "relay_write_effects_executed_twice",
        "relay_http_request_seconds",
    ):
        assert family in body, family


def test_http_latency_uses_the_route_template_not_the_raw_path(client: TestClient) -> None:
    run_id = uuid.uuid4()
    client.get(f"/v1/runs/{run_id}")  # 401, but still observed
    body = client.get("/metrics").text
    assert 'route="/v1/runs/{run_id}"' in body
    assert str(run_id) not in body  # raw ids would explode label cardinality


def test_ops_json_matches_the_dashboard_contract(client: TestClient) -> None:
    s = client.get("/ops.json").json()
    for key in ("runs", "steps", "spend_usd", "breakers", "repaired_finals", "write_effects"):
        assert key in s
    assert s["write_effects"]["executed_twice"] == 0  # the invariant, on live data


def test_ops_page_is_served(client: TestClient) -> None:
    r = client.get("/ops")
    assert r.status_code == 200 and "Relay ops" in r.text


def test_trace_id_lands_on_ledger_rows(parked_run: tuple[uuid.UUID, str]) -> None:
    from relay.db import pool

    run_id, _ = parked_run
    setup("relay-test")
    assert current_trace_id() is None  # outside a span: NULL, not garbage
    with tracer().start_as_current_span("relay.run_attempt"):
        inside = current_trace_id()
        assert inside is not None and len(inside) == 32 and inside != "0" * 32
        append_step(run_id, 1, "error", {"reason": "trace-test"}, "pytest")

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT trace_id FROM steps WHERE run_id = %s AND step_index = 1", (run_id,)
        ).fetchone()
    assert row is not None and row[0] == inside
