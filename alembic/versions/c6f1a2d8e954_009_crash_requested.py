"""009 crash requested

Revision ID: c6f1a2d8e954
Revises: b9e4f7a2d318
Create Date: 2026-10-29

"""

from alembic import op

revision = "c6f1a2d8e954"
down_revision = "b9e4f7a2d318"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Demo page: "Kill the worker" sets this timestamp; the owning worker sees
    # it at its next heartbeat, clears it (one-shot) and exits like `docker
    # kill` would — no cleanup, lease left to expire, another worker takes
    # over by replaying the ledger. Only settable through a demo-mode tenant's
    # key (the API refuses for live tenants), so it can never waste paid calls.
    op.execute("ALTER TABLE runs ADD COLUMN crash_requested_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS crash_requested_at")
