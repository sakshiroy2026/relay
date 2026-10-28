"""004 step kinds for the repair ladder and the dead-letter queue

Revision ID: d4a7c3b21e09
Revises: b3e8f2a19c47
Create Date: 2026-10-28

"""

from alembic import op

revision = "d4a7c3b21e09"
down_revision = "b3e8f2a19c47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Two new ledger row kinds (Day 9). No tables: the DLQ is the runs table
    # itself ('dead' has been in run_status since migration 001), and a requeue
    # is an append-only marker in the ledger, like everything else.
    #   repair_requested — the final answer failed validation; this row holds the
    #                      correction message sent back to the model, so replay
    #                      can rebuild the conversation word for word.
    #   requeued         — an operator put a failed/dead run back in the queue;
    #                      replay treats the verdict before it as cleared.
    op.execute("ALTER TYPE step_kind ADD VALUE IF NOT EXISTS 'repair_requested'")
    op.execute("ALTER TYPE step_kind ADD VALUE IF NOT EXISTS 'requeued'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum. Rows of these kinds would
    # also still exist. Leave the values in place; they are harmless unused.
    pass
