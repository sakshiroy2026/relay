"""002 companies and tool_invocations

Revision ID: 7c2e9a41d5b3
Revises: 51c83862a121
Create Date: 2026-09-30

"""

from alembic import op

revision = "7c2e9a41d5b3"
down_revision = "51c83862a121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── tool_invocations: one row per write-tool call, keyed BEFORE it runs ──
    op.execute("""
        CREATE TABLE tool_invocations (
            idem_key     TEXT PRIMARY KEY,        -- '<run_id>:<tool_call_id>'
            run_id       UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_index   INT  NOT NULL,           -- index of the FIRST tool_call row
            tool_name    TEXT NOT NULL,
            result       JSONB,
            status       TEXT NOT NULL,           -- 'in_flight' | 'ok'
            attempts     INT  NOT NULL DEFAULT 1, -- how many times a worker asked
            executions   INT  NOT NULL DEFAULT 0, -- how many times the effect committed
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ
        )
    """)

    # ── companies: the agent's output; UNIQUE(tenant_id, domain) is the last line of defence
    op.execute("""
        CREATE TABLE companies (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID NOT NULL REFERENCES tenants(id),
            domain         TEXT NOT NULL,
            name           TEXT NOT NULL,
            hq_country     TEXT,
            founded_year   INT,
            industry       TEXT,
            employee_range TEXT,
            funding_stage  TEXT,
            description    TEXT,
            sources        JSONB NOT NULL DEFAULT '[]',
            confidence     JSONB NOT NULL DEFAULT '{}',
            created_by_run UUID REFERENCES runs(id),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT companies_tenant_domain_uniq UNIQUE (tenant_id, domain)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS companies")
    op.execute("DROP TABLE IF EXISTS tool_invocations")
