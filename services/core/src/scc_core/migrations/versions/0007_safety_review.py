"""Safety review queue (P3.03): who reviewed a flagged job, when, and what they found.

The review lives on the job. `needs_review` stays the queue's one flag: reviewing clears it, and a
later flag (an edited claim that adds a topic) sets it again, so the job comes back to the queue
with its earlier review still shown.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

OUTCOMES = ("notice_right", "notice_not_needed", "follow_up")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core.jobs"
        " ADD COLUMN reviewed_at timestamptz,"
        " ADD COLUMN reviewed_by bigint,"
        " ADD COLUMN review_outcome text,"
        " ADD COLUMN review_note text,"
        f" ADD CONSTRAINT jobs_review_outcome_valid CHECK (review_outcome IN {OUTCOMES!r}),"
        " ADD CONSTRAINT jobs_review_note_length CHECK (length(review_note) <= 500),"
        " ADD CONSTRAINT jobs_review_complete CHECK ("
        "(reviewed_at IS NULL) = (reviewed_by IS NULL)"
        " AND (reviewed_at IS NULL) = (review_outcome IS NULL))"
    )
    op.execute(
        "CREATE INDEX jobs_reviewed ON core.jobs (reviewed_at DESC) WHERE reviewed_at IS NOT NULL"
    )
    op.execute("CREATE INDEX injection_events_at ON core.injection_events (at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX core.injection_events_at")
    op.execute("DROP INDEX core.jobs_reviewed")
    op.execute(
        "ALTER TABLE core.jobs DROP CONSTRAINT jobs_review_complete,"
        " DROP CONSTRAINT jobs_review_note_length, DROP CONSTRAINT jobs_review_outcome_valid,"
        " DROP COLUMN review_note, DROP COLUMN review_outcome, DROP COLUMN reviewed_by,"
        " DROP COLUMN reviewed_at"
    )
