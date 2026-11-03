"""Guardrails (P3.01-P3.03, D-050): the needs_clarification state, review flags, injection events.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

STATES = (
    "submitted",
    "extracting",
    "awaiting_confirmation",
    "searching",
    "retrieving",
    "assessing",
    "verifying",
    "scoring",
    "done",
    "failed",
    "refused",
    "needs_clarification",
)


def upgrade() -> None:
    op.execute("ALTER TABLE core.jobs DROP CONSTRAINT jobs_state_valid")
    op.execute(
        f"ALTER TABLE core.jobs ADD CONSTRAINT jobs_state_valid CHECK (state IN {STATES!r})"
    )
    # Safety topics from triage and the admin review queue (P3.03).
    op.execute(
        "ALTER TABLE core.jobs ADD COLUMN sensitive_topics text[] NOT NULL DEFAULT '{}',"
        " ADD COLUMN needs_review boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE INDEX jobs_needs_review ON core.jobs (created_at DESC) WHERE needs_review"
    )
    # Instructions found in submitted or fetched text (P3.02). Never the text itself: the job holds that.
    op.execute(
        """
        CREATE TABLE core.injection_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id uuid NOT NULL REFERENCES core.jobs (id) ON DELETE CASCADE,
            user_id bigint NOT NULL,
            stage text NOT NULL,
            source text NOT NULL,
            config_version text NOT NULL,
            at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX injection_events_user ON core.injection_events (user_id, at) WHERE source = 'submitted_text'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE core.injection_events")
    op.execute(
        "ALTER TABLE core.jobs DROP COLUMN sensitive_topics, DROP COLUMN needs_review"
    )
    op.execute("ALTER TABLE core.jobs DROP CONSTRAINT jobs_state_valid")
    op.execute(
        f"ALTER TABLE core.jobs ADD CONSTRAINT jobs_state_valid CHECK (state IN {STATES[:-1]!r})"
    )
