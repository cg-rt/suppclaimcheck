"""Article links (P3.12, D-055): a check can start from a web page's address.

- `jobs.input_type` is now 'text' or 'url' (every existing check is 'text').
- `jobs.source`: what the gateway read for a link: the address after redirects, the title, the site
  name, and how much text there was and how much the model read.
- `jobs.source_text`: the text the model read, so the claims can be traced back to it. Deleted with
  the check, like the submitted text (docs/PRIVACY.md).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.jobs
            ADD COLUMN source jsonb,
            ADD COLUMN source_text text,
            ADD CONSTRAINT jobs_input_type_valid CHECK (input_type IN ('text', 'url')),
            ADD CONSTRAINT jobs_source_text_length CHECK (length(source_text) <= 200000),
            ADD CONSTRAINT jobs_source_only_for_links CHECK (
                input_type = 'url' OR (source IS NULL AND source_text IS NULL)
            )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.jobs
            DROP CONSTRAINT jobs_source_only_for_links,
            DROP CONSTRAINT jobs_source_text_length,
            DROP CONSTRAINT jobs_input_type_valid,
            DROP COLUMN source_text,
            DROP COLUMN source
        """
    )
