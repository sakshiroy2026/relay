"""The honesty check on a public real-AI record: which listed sources did it never open?

Run `ef6ebda7` on Render cited https://stripe.com/about while its only fetch failed, and
the page showed that as a source with nothing beside it. No model or tool runs here:
the ledger rows are written by hand.
"""

import json
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from relay.api.events import run_event_stream
from relay.api.routes.runs import unread_sources
from relay.config import settings
from scripts.seed import create_api_key

ABOUT = "https://stripe.com/about"
HOME = "https://stripe.com/"


def real_run(*sources: str, mode: str = "public_real") -> dict[str, Any]:
    return {"input": {"domain": "stripe.com", "mode": mode}, "result": {"sources": list(sources)}}


def fetch(url: str, ok: bool, call: str = "c1") -> list[dict[str, Any]]:
    """The two ledger rows of one fetch_page call."""
    return [
        {
            "kind": "tool_call",
            "payload": {"tool": "fetch_page", "args": {"url": url}, "tool_call_id": call},
        },
        {
            "kind": "tool_result",
            "payload": {"tool": "fetch_page", "tool_call_id": call, "ok": ok, "content": "x"},
        },
    ]


def test_a_source_that_was_never_fetched_is_named() -> None:
    assert unread_sources(real_run(ABOUT), []) == [ABOUT]


def test_a_failed_fetch_of_that_page_does_not_count_as_reading_it() -> None:
    assert unread_sources(real_run(HOME), fetch("https://stripe.com", ok=False)) == [HOME]


def test_a_page_really_opened_is_not_named_even_with_a_different_slash() -> None:
    assert unread_sources(real_run(HOME), fetch("https://stripe.com", ok=True)) == []


def test_only_the_unopened_source_is_named() -> None:
    assert unread_sources(real_run(HOME, ABOUT), fetch(HOME, ok=True)) == [ABOUT]


def test_another_tool_with_the_same_call_id_does_not_count() -> None:
    rows = [
        {
            "kind": "tool_result",
            "payload": {"tool": "web_search", "tool_call_id": "c1", "ok": True, "content": ABOUT},
        }
    ]
    assert unread_sources(real_run(ABOUT), rows) == [ABOUT]


def test_other_modes_and_unfinished_runs_are_not_checked() -> None:
    assert unread_sources(real_run(ABOUT, mode="demo"), []) == []
    assert unread_sources(real_run(ABOUT, mode="live"), []) == []
    assert unread_sources({"input": {"mode": "public_real"}, "result": None}, []) == []


def finish_as_real_run(run_id: uuid.UUID) -> None:
    """Turn the parked run into a finished public real-AI run with one failed fetch."""
    from relay.db import pool

    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET input = input || %s, result = %s WHERE id = %s",
            (Jsonb({"mode": "public_real"}), Jsonb({"sources": [ABOUT]}), run_id),
        )
        for index, row in enumerate(fetch(HOME, ok=False), start=1):
            conn.execute(
                "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
                "VALUES (%s, %s, %s, %s, 'pytest')",
                (run_id, index, row["kind"], Jsonb(row["payload"])),
            )


def test_the_run_route_carries_the_unread_sources(parked_run: tuple[uuid.UUID, str]) -> None:
    from fastapi.testclient import TestClient

    from relay.api.main import app
    from relay.db import pool

    run_id, _ = parked_run
    finish_as_real_run(run_id)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            key = create_api_key(cur, settings.dev_tenant_id)
    with TestClient(app) as client:
        r = client.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["unread_sources"] == [ABOUT]


def test_the_stream_carries_them_too(parked_run: tuple[uuid.UUID, str]) -> None:
    run_id, _ = parked_run
    finish_as_real_run(run_id)
    first = next(run_event_stream(uuid.UUID(settings.dev_tenant_id), run_id))
    data = next(line[6:] for line in first.splitlines() if line.startswith("data: "))
    assert json.loads(data)["run"]["unread_sources"] == [ABOUT]


def test_the_page_shows_them_beside_the_record(db: None) -> None:
    from fastapi.testclient import TestClient

    from relay.api.main import app

    page = TestClient(app).get("/").text
    assert 'id="basis"' in page
    assert "run.unread_sources" in page
    assert "Not researched" in page
    assert "says which fields it couldn't source" not in page  # the model doesn't reliably
