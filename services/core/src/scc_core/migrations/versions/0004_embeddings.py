"""Embedding cache for hybrid retrieval (P2.06, D-019, D-046).

One vector per passage text and model, keyed by the text's SHA-256, so an article read again is not
embedded again. bge-small-en-v1.5 has 384 dimensions; another model gets another table or column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE core.embeddings (
            text_hash text NOT NULL,
            model text NOT NULL,
            embedding public.vector(384) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (text_hash, model)
        )
        """
    )


def downgrade() -> None:
    op.drop_table("embeddings", schema="core")
