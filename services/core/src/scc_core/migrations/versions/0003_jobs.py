"""Jobs, their passages and hallucination events (P1.05, P1.08, P1.09).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STATES = (
    "submitted",
    "extracting",
    "awaiting_confirmation",
    "searching",
    "retrieving",
    "assessing",
    "verifying",
    "scoring",
    "done",
    "failed",
    "refused",
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column(
            "id", pg.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        # The user lives in web's auth schema, which core may not read; no foreign key across schemas.
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="submitted"),
        sa.Column("input_type", sa.Text, nullable=False),
        sa.Column("input_text", sa.Text, nullable=False),
        sa.Column("extracted_claims", pg.JSONB),
        sa.Column("confirmed_claim", pg.JSONB),
        sa.Column("result", pg.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("config_version", sa.Text, nullable=False),
        sa.Column("correlation_id", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(f"state IN {STATES!r}", name="jobs_state_valid"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="jobs_idempotency"),
        schema="core",
    )
    op.create_index(
        "jobs_user_created",
        "jobs",
        ["user_id", sa.text("created_at DESC")],
        schema="core",
    )

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "job_id",
            pg.UUID,
            sa.ForeignKey("core.jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="core",
    )
    op.create_index("job_events_job", "job_events", ["job_id", "id"], schema="core")

    op.create_table(
        "passages",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "job_id",
            pg.UUID,
            sa.ForeignKey("core.jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pmcid", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("section", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column(
            "tsv",
            pg.TSVECTOR,
            sa.Computed("to_tsvector('english', text)", persisted=True),
        ),
        schema="core",
    )
    op.create_index(
        "passages_job", "passages", ["job_id", "pmcid", "ordinal"], schema="core"
    )
    op.create_index(
        "passages_tsv", "passages", ["tsv"], postgresql_using="gin", schema="core"
    )

    op.create_table(
        "hallucination_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "job_id",
            pg.UUID,
            sa.ForeignKey("core.jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("passage_ref", sa.Text),
        sa.Column("quote", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("config_version", sa.Text, nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="core",
    )


def downgrade() -> None:
    for table in ("hallucination_events", "passages", "job_events", "jobs"):
        op.drop_table(table, schema="core")
