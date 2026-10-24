"""The Procrastinate job queue, inside the core schema (P1.05, D-014).

Procrastinate's schema uses unqualified names, so it is applied with the search path set to core.
Later Procrastinate upgrades ship their own SQL migrations; each becomes an Alembic migration here.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""

from alembic import op
from procrastinate.schema import SchemaManager

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL search_path TO core")
    op.execute(SchemaManager.get_schema())
    op.execute("RESET search_path")


def downgrade() -> None:
    raise NotImplementedError(
        "drop the procrastinate objects by hand if this is ever needed"
    )
