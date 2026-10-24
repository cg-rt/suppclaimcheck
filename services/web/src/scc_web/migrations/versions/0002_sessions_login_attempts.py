"""Server-side sessions and sign-in attempts (P1.04, D-016).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        # Only a SHA-256 of the session ID is stored, so a copy of this table can't sign anyone in.
        sa.Column("id_hash", sa.Text, primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("auth.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("csrf_token", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        schema="auth",
    )
    op.create_index("sessions_user", "sessions", ["user_id"], schema="auth")

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("email_lower", sa.Text, nullable=False),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("succeeded", sa.Boolean, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        schema="auth",
    )
    op.create_index(
        "login_attempts_email", "login_attempts", ["email_lower", "at"], schema="auth"
    )
    op.create_index(
        "login_attempts_address", "login_attempts", ["address", "at"], schema="auth"
    )


def downgrade() -> None:
    op.drop_table("login_attempts", schema="auth")
    op.drop_table("sessions", schema="auth")
