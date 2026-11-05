"""Account deletion (P3.06, D-054).

`deletion_requested_at` marks an account its owner (or an admin, `deletion_requested_by`) asked to
delete. The account is
signed out and disabled at once; its checks are deleted in core, then the row itself goes. If any
step fails, web's hourly purge finishes the deletion.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auth.users ADD COLUMN deletion_requested_at timestamptz,"
        " ADD COLUMN deletion_requested_by bigint"
    )
    op.execute(
        "CREATE INDEX users_deletion_requested ON auth.users (deletion_requested_at)"
        " WHERE deletion_requested_at IS NOT NULL"
    )
    op.execute("CREATE INDEX login_attempts_at ON auth.login_attempts (at)")


def downgrade() -> None:
    op.execute("DROP INDEX auth.login_attempts_at")
    op.execute(
        "ALTER TABLE auth.users DROP COLUMN deletion_requested_at, DROP COLUMN deletion_requested_by"
    )
