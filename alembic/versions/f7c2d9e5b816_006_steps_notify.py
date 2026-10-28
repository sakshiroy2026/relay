"""006 notify on ledger appends

Revision ID: f7c2d9e5b816
Revises: e5b1f8c4a203
Create Date: 2026-10-28

"""

from alembic import op

revision = "f7c2d9e5b816"
down_revision = "e5b1f8c4a203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Day 11: every ledger append rings a bell. pg_notify fires on COMMIT (a
    # rolled-back insert rings nothing), the payload is the run id, and nobody
    # has to poll the steps table to notice progress. A trigger, not app code:
    # the harnesses' direct inserts ring it too, and append_step stays untouched.
    op.execute("""
        CREATE FUNCTION notify_step() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('relay_steps', NEW.run_id::text);
            RETURN NULL;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER steps_notify
            AFTER INSERT ON steps
            FOR EACH ROW EXECUTE FUNCTION notify_step()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS steps_notify ON steps")
    op.execute("DROP FUNCTION IF EXISTS notify_step()")
