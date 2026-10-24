"""auth.users: invite-only accounts (P1.03; the login code arrives with P1.04).

Revision ID: 0001
Revises:
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="user"),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("role IN ('user', 'admin')", name="users_role_valid"),
        schema="auth",
    )
    op.create_index(
        "users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
        schema="auth",
    )


def downgrade() -> None:
    op.drop_table("users", schema="auth")
