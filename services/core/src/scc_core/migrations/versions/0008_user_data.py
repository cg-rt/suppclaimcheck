"""User data (P3.06, P4.03, P4.04, D-054): feedback on results, and when each check was confirmed and finished.

- `feedback`: one rating per check, from the person who ran it. Deleted with the check.
- `jobs.confirmed_at` and `jobs.finished_at`, set by a trigger on every state change, whichever code
  path makes it (pipeline, confirmation, the stalled sweep, admin retry). A retry clears `finished_at`
  until the check finishes again. Existing checks are filled in from their events.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

FINISHED = "('done', 'failed', 'refused', 'needs_clarification')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE core.feedback (
            job_id uuid PRIMARY KEY REFERENCES core.jobs (id) ON DELETE CASCADE,
            user_id bigint NOT NULL,
            verdict_right text CHECK (verdict_right IN ('yes', 'no', 'unsure')),
            understandable text CHECK (understandable IN ('yes', 'partly', 'no')),
            missing text CHECK (length(missing) <= 1000),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT feedback_not_empty CHECK (
                verdict_right IS NOT NULL OR understandable IS NOT NULL OR missing IS NOT NULL
            )
        )
        """
    )
    op.execute("CREATE INDEX feedback_updated ON core.feedback (updated_at DESC)")
    op.execute(
        "ALTER TABLE core.jobs ADD COLUMN confirmed_at timestamptz, ADD COLUMN finished_at timestamptz"
    )
    op.execute(
        f"""
        CREATE FUNCTION core.jobs_timing() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.state IN {FINISHED} THEN
                IF NEW.state IS DISTINCT FROM OLD.state OR NEW.finished_at IS NULL THEN
                    NEW.finished_at := now();
                END IF;
            ELSE
                NEW.finished_at := NULL;
            END IF;
            IF NEW.confirmed_claim IS NOT NULL AND OLD.confirmed_claim IS NULL THEN
                NEW.confirmed_at := now();
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER jobs_timing BEFORE UPDATE OF state, confirmed_claim ON core.jobs"
        " FOR EACH ROW EXECUTE FUNCTION core.jobs_timing()"
    )
    # Backfill from the events. Disabling the trigger keeps it from overwriting what is filled in.
    op.execute("ALTER TABLE core.jobs DISABLE TRIGGER jobs_timing")
    op.execute(
        """
        UPDATE core.jobs j SET confirmed_at = e.at
        FROM (SELECT job_id, min(at) AS at FROM core.job_events
              WHERE state = 'searching' AND detail = 'confirmed' GROUP BY job_id) e
        WHERE e.job_id = j.id
        """
    )
    op.execute(
        f"""
        UPDATE core.jobs j SET finished_at = e.at
        FROM (SELECT DISTINCT ON (job_id) job_id, at FROM core.job_events
              ORDER BY job_id, id DESC) e
        WHERE e.job_id = j.id AND j.state IN {FINISHED}
        """
    )
    op.execute("ALTER TABLE core.jobs ENABLE TRIGGER jobs_timing")
    op.execute(
        f"CREATE INDEX jobs_finished ON core.jobs (finished_at DESC) WHERE state IN {FINISHED}"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER jobs_timing ON core.jobs")
    op.execute("DROP FUNCTION core.jobs_timing()")
    op.execute(
        "ALTER TABLE core.jobs DROP COLUMN confirmed_at, DROP COLUMN finished_at"
    )
    op.execute("DROP TABLE core.feedback")
