"""Versioned config store (P2.09, D-048): versions, evals, audit and the active pointer.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02
"""

from alembic import op

from scc_common.config_store import drop_sql, tables_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in tables_sql("auth"):
        op.execute(statement)


def downgrade() -> None:
    for statement in drop_sql("auth"):
        op.execute(statement)
