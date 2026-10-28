"""005 per-tenant limits

Revision ID: e5b1f8c4a203
Revises: d4a7c3b21e09
Create Date: 2026-10-28

"""

from alembic import op

revision = "e5b1f8c4a203"
down_revision = "d4a7c3b21e09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Day 10: every tenant gets its own ceilings. Defaults are generous enough
    # for the demo and small enough that a runaway client cannot drain a budget.
    op.execute("""
        ALTER TABLE tenants
            ADD COLUMN max_concurrent_runs INT NOT NULL DEFAULT 25,
            ADD COLUMN requests_per_minute INT NOT NULL DEFAULT 120
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tenants
            DROP COLUMN IF EXISTS max_concurrent_runs,
            DROP COLUMN IF EXISTS requests_per_minute
    """)
