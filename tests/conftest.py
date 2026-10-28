"""Shared fixtures. Database tests skip cleanly when no database is reachable."""

import uuid
from collections.abc import Iterator

import pytest

from relay.config import settings


def _db_available() -> bool:
    try:
        from relay.db import pool

        with pool.connection() as conn:
            conn.execute("SELECT 1 FROM tool_invocations LIMIT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def db() -> None:
    if not _db_available():
        pytest.skip("no database with migrations reachable (set DATABASE_URL / alembic upgrade)")


@pytest.fixture
def parked_run(db: None) -> Iterator[tuple[uuid.UUID, str]]:
    """A run with its run_started step, parked as 'dead' so no live worker claims it."""
    from psycopg.types.json import Jsonb

    from relay.db import pool

    domain = f"pytest-{uuid.uuid4().hex[:8]}.example"
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO runs (tenant_id, input, agent_version, status) "
            "VALUES (%s, %s, 'pytest', 'dead') RETURNING id",
            (settings.dev_tenant_id, Jsonb({"domain": domain})),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO steps (run_id, step_index, kind, payload, written_by) "
            "VALUES (%s, 0, 'run_started', %s, 'pytest')",
            (row[0], Jsonb({"domain": domain})),
        )
    yield row[0], domain
