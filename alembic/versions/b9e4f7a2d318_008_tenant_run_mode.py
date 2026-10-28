"""008 tenant run mode

Revision ID: b9e4f7a2d318
Revises: a8d3e6f1c927
Create Date: 2026-10-28

"""

from alembic import op

revision = "b9e4f7a2d318"
down_revision = "a8d3e6f1c927"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # P1: which model and tools a tenant's runs use. 'demo' (the default, and
    # every existing tenant) = the scripted fake model + fake tools: $0, safe
    # for a public link. 'live' = the real model + real tools: costs money, so
    # it has to be switched on per tenant, on purpose. The API copies the mode
    # into runs.input when a run is created, so a run keeps its mode even if
    # the tenant's setting changes while it is in flight.
    op.execute("""
        ALTER TABLE tenants
            ADD COLUMN run_mode TEXT NOT NULL DEFAULT 'demo'
                CHECK (run_mode IN ('demo', 'live'))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS run_mode")
