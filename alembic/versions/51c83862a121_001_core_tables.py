"""001 core tables

Revision ID: 51c83862a121
Revises:
Create Date: auto

"""
from alembic import op

revision = "51c83862a121"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── enums ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TYPE run_status AS ENUM (
            'pending',
            'running',
            'awaiting_approval',
            'succeeded',
            'failed',
            'dead'
        )
    """)

    op.execute("""
        CREATE TYPE step_kind AS ENUM (
            'run_started',
            'model_call',
            'tool_call',
            'tool_result',
            'approval_requested',
            'approval_resolved',
            'budget_exceeded',
            'error',
            'final'
        )
    """)

    # ── tenants ────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE tenants (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # ── api_keys ───────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE api_keys (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            key_prefix TEXT NOT NULL UNIQUE,
            key_hash   TEXT NOT NULL,
            name       TEXT NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX api_keys_active_prefix_idx
            ON api_keys (key_prefix)
            WHERE revoked_at IS NULL
    """)

    # ── runs ───────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE runs (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(id),
            status           run_status NOT NULL DEFAULT 'pending',
            input            JSONB NOT NULL,
            result           JSONB,
            error            JSONB,

            lease_owner      TEXT,
            lease_expires_at TIMESTAMPTZ,
            attempt_count    INT NOT NULL DEFAULT 0,
            max_attempts     INT NOT NULL DEFAULT 5,

            next_step_index  INT NOT NULL DEFAULT 0,

            budget_usd       NUMERIC(10,6) NOT NULL DEFAULT 0.25,
            spent_usd        NUMERIC(10,6) NOT NULL DEFAULT 0,
            tokens_in        BIGINT NOT NULL DEFAULT 0,
            tokens_out       BIGINT NOT NULL DEFAULT 0,

            agent_version    TEXT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at       TIMESTAMPTZ,
            finished_at      TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE INDEX runs_claimable_idx
            ON runs (created_at)
            WHERE status IN ('pending', 'running')
    """)
    op.execute("""
        CREATE INDEX runs_tenant_created_idx
            ON runs (tenant_id, created_at DESC)
    """)

    # ── steps ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE steps (
            id          BIGSERIAL PRIMARY KEY,
            run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_index  INT NOT NULL,
            kind        step_kind NOT NULL,
            payload     JSONB NOT NULL,
            written_by  TEXT NOT NULL,
            trace_id    TEXT,
            duration_ms INT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT steps_run_index_uniq UNIQUE (run_id, step_index)
        )
    """)
    op.execute("CREATE INDEX steps_run_idx ON steps (run_id, step_index)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS steps")
    op.execute("DROP TABLE IF EXISTS runs")
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP TABLE IF EXISTS tenants")
    op.execute("DROP TYPE IF EXISTS step_kind")
    op.execute("DROP TYPE IF EXISTS run_status")