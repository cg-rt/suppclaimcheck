"""Budget guard and idempotent LLM calls (P3.04, P3.05, D-053).

- `llm_calls.idempotency_key`: a call core sends again with the same key (a re-run of the same job
  step) gets the stored answer instead of a new, charged call. One live or finished call per key.
- `llm_calls.output`: the stored answer of a keyed call, kept for `output_retention_days` (config).
- `budget_alerts`: each warning level is sent once per lane and month.
- An index for the per-user daily quota, which counts each account's extractions per UTC day.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gateway.llm_calls ADD COLUMN idempotency_key text, ADD COLUMN output jsonb,"
        " ADD CONSTRAINT llm_calls_idempotency_key_length CHECK (length(idempotency_key) <= 128)"
    )
    # Failed calls don't hold their key: trying again after a failure must make a new call.
    op.execute(
        "CREATE UNIQUE INDEX llm_calls_idempotency ON gateway.llm_calls (idempotency_key)"
        " WHERE idempotency_key IS NOT NULL AND status IN ('reserved', 'ok')"
    )
    op.execute(
        "CREATE INDEX llm_calls_user_extract ON gateway.llm_calls (user_id, created_at)"
        " WHERE stage = 'extract' AND lane = 'live'"
    )
    op.execute(
        """
        CREATE TABLE gateway.budget_alerts (
            lane text NOT NULL,
            month date NOT NULL,
            level text NOT NULL,
            spent_eur numeric(12, 6) NOT NULL,
            sent_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (lane, month, level)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE gateway.budget_alerts")
    op.execute("DROP INDEX gateway.llm_calls_user_extract")
    op.execute("DROP INDEX gateway.llm_calls_idempotency")
    op.execute(
        "ALTER TABLE gateway.llm_calls DROP CONSTRAINT llm_calls_idempotency_key_length,"
        " DROP COLUMN output, DROP COLUMN idempotency_key"
    )
