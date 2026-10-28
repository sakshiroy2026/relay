"""007 client idempotency keys

Revision ID: a8d3e6f1c927
Revises: f7c2d9e5b816
Create Date: 2026-10-28

"""

from alembic import op

revision = "a8d3e6f1c927"
down_revision = "f7c2d9e5b816"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The first of the three idempotency layers (README, "The four mechanisms"):
    # a client's Idempotency-Key on POST /v1/runs maps to the run it created.
    # Keyed per tenant, so two tenants may use the same key string. The row is
    # inserted in the SAME transaction as the run and its step 0, so a key
    # without its run (or a run without its key) cannot exist.
    op.execute("""
        CREATE TABLE idempotency_keys (
            tenant_id    UUID NOT NULL REFERENCES tenants(id),
            key          TEXT NOT NULL,
            request_hash TEXT NOT NULL,  -- sha256 of the body: same key + other body = 422
            run_id       UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

            PRIMARY KEY (tenant_id, key)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
