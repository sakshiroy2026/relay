"""Circuit-breaker tests against a real database (the state IS the database).

Each test uses its own uniquely named tool, so parallel runs and the real
tools' rows never collide. Skipped cleanly when no database is reachable.
"""

import uuid
from collections.abc import Iterator

import pytest

from relay.config import settings


def _db_available() -> bool:
    try:
        from relay.db import pool

        with pool.connection() as conn:
            conn.execute("SELECT 1 FROM circuit_breakers LIMIT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="no database with migration 003 reachable (set DATABASE_URL / run alembic upgrade head)",
)


@pytest.fixture
def tool() -> Iterator[str]:
    """A unique tool name per test; its row is removed afterwards."""
    from relay.db import pool

    name = f"testtool_{uuid.uuid4().hex[:8]}"
    yield name
    with pool.connection() as conn:
        conn.execute("DELETE FROM circuit_breakers WHERE tool = %s", (name,))


def _set(tool: str, state: str, *, opened_ago: float = 0.0, updated_ago: float = 0.0) -> None:
    """Write a breaker row directly, with timestamps shifted into the past."""
    from relay.db import pool

    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO circuit_breakers (tool, state, consecutive_failures, opened_at, updated_at)
            VALUES (%(tool)s, %(state)s, %(threshold)s,
                    now() - %(opened_ago)s * interval '1 second',
                    now() - %(updated_ago)s * interval '1 second')
            ON CONFLICT (tool) DO UPDATE SET
                state = EXCLUDED.state,
                opened_at = EXCLUDED.opened_at,
                updated_at = EXCLUDED.updated_at
            """,
            {
                "tool": tool,
                "state": state,
                "threshold": settings.breaker_threshold,
                "opened_ago": opened_ago,
                "updated_ago": updated_ago,
            },
        )


def _state(tool: str) -> str | None:
    from relay.db import pool

    with pool.connection() as conn:
        row = conn.execute("SELECT state FROM circuit_breakers WHERE tool = %s", (tool,)).fetchone()
    return None if row is None else row[0]


def test_unknown_tool_is_allowed_and_rowless(tool: str) -> None:
    from relay.core import breaker

    assert breaker.allow(tool) is True
    assert _state(tool) is None  # allow() never writes for a healthy tool


def test_success_on_a_clean_tool_writes_nothing(tool: str) -> None:
    from relay.core import breaker

    breaker.record_success(tool)
    assert _state(tool) is None


def test_opens_after_threshold_consecutive_failures(tool: str) -> None:
    from relay.core import breaker

    for n in range(settings.breaker_threshold - 1):
        breaker.record_failure(tool)
        assert _state(tool) == "closed", f"opened too early, after {n + 1} failures"
        assert breaker.allow(tool) is True

    breaker.record_failure(tool)  # the fifth one trips it
    assert _state(tool) == "open"
    assert breaker.allow(tool) is False  # fresh open: nobody gets through


def test_success_resets_the_failure_count(tool: str) -> None:
    from relay.core import breaker

    for _ in range(settings.breaker_threshold - 1):
        breaker.record_failure(tool)
    breaker.record_success(tool)  # one success wipes the streak
    for _ in range(settings.breaker_threshold - 1):
        breaker.record_failure(tool)
    assert _state(tool) == "closed"  # 4 + 4 non-consecutive failures never trip it


def test_expired_open_lets_exactly_one_probe_through(tool: str) -> None:
    from relay.core import breaker

    _set(tool, "open", opened_ago=settings.breaker_open_seconds + 1)
    assert breaker.allow(tool) is True  # this caller becomes the probe
    assert _state(tool) == "half_open"
    assert breaker.allow(tool) is False  # a second caller while the probe is out


def test_probe_success_closes_for_everyone(tool: str) -> None:
    from relay.core import breaker

    _set(tool, "half_open")
    breaker.record_success(tool)
    assert _state(tool) == "closed"
    assert breaker.allow(tool) is True


def test_probe_failure_reopens(tool: str) -> None:
    from relay.core import breaker

    _set(tool, "half_open")
    breaker.record_failure(tool)  # one failure in half_open is enough
    assert _state(tool) == "open"
    assert breaker.allow(tool) is False


def test_a_probe_whose_worker_died_expires(tool: str) -> None:
    from relay.core import breaker

    # the probe went out, the worker holding it was killed, time passed
    _set(tool, "half_open", updated_ago=settings.breaker_open_seconds + 1)
    assert breaker.allow(tool) is True  # a new probe is allowed; not jammed forever
    assert _state(tool) == "half_open"
