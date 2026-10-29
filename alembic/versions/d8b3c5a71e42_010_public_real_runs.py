"""010 public real-AI runs

Revision ID: d8b3c5a71e42
Revises: c6f1a2d8e954
Create Date: 2026-10-29

"""

from alembic import op

revision = "d8b3c5a71e42"
down_revision = "c6f1a2d8e954"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # P7, Option A: a public visitor may opt one run into the real (free-tier)
    # model. Those runs are stored like any other, with mode 'public_real' in
    # runs.input, so the day's count is just a count of rows — no new table and
    # nothing to keep in sync with the runs themselves.
    #
    # The index is what makes that count cheap, and it is the whole migration:
    # additive, no column, no data rewritten. It is partial, so it stays tiny
    # however many demo runs the table holds.
    #
    # The count runs inside create_run's per-tenant advisory lock, so two
    # visitors racing for the last run of the day cannot both get it.
    op.execute("""
        CREATE INDEX IF NOT EXISTS runs_public_real_day_idx
            ON runs (tenant_id, created_at)
            WHERE (input->>'mode') = 'public_real'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS runs_public_real_day_idx")
