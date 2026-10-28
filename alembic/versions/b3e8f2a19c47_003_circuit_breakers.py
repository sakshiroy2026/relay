"""003 circuit breakers

Revision ID: b3e8f2a19c47
Revises: 7c2e9a41d5b3
Create Date: 2026-10-28

"""

from alembic import op

revision = "b3e8f2a19c47"
down_revision = "7c2e9a41d5b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── circuit_breakers: one row per tool, shared by every worker ──────────
    # A tool with no row is simply closed (healthy). Rows appear on the first
    # failure and are never deleted; core tables are untouched.
    op.execute("""
        CREATE TABLE circuit_breakers (
            tool                 TEXT PRIMARY KEY,
            state                TEXT NOT NULL DEFAULT 'closed'
                                 CHECK (state IN ('closed', 'open', 'half_open')),
            consecutive_failures INT  NOT NULL DEFAULT 0,
            opened_at            TIMESTAMPTZ,          -- when it last tripped open
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE circuit_breakers")
