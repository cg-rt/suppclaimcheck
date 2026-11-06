"""Privacy (P3.06, D-054): the accounts that have been deleted.

`erased_users` holds only the web user ID of a deleted account (no email, nothing else), so the
purge can unlink an LLM call that was still running when the account was deleted.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE gateway.erased_users (
            user_id bigint PRIMARY KEY,
            erased_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE gateway.erased_users")
