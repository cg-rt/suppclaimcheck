"""LLM call records and the article cache (P1.06, P1.07).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per LLM call: model, tokens, cost, latency, stage, job and user (AGENTS.md section 8).
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("lane", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("job_id", sa.Text),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        # While a call runs this holds its reserved worst-case cost; afterwards, the real cost.
        sa.Column("cost_eur", sa.Numeric(12, 6), nullable=False),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("correlation_id", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("lane IN ('live', 'eval')", name="llm_calls_lane_valid"),
        sa.CheckConstraint(
            "status IN ('reserved', 'ok', 'failed')", name="llm_calls_status_valid"
        ),
        schema="gateway",
    )
    op.create_index(
        "llm_calls_lane_created", "llm_calls", ["lane", "created_at"], schema="gateway"
    )

    op.create_table(
        "articles",
        sa.Column("pmid", sa.Text, primary_key=True),
        sa.Column("pmcid", sa.Text),
        sa.Column("license", sa.Text),
        sa.Column("metadata", pg.JSONB, nullable=False),
        sa.Column("fulltext_xml", sa.Text),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="gateway",
    )


def downgrade() -> None:
    op.drop_table("articles", schema="gateway")
    op.drop_table("llm_calls", schema="gateway")
