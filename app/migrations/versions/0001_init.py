"""init

Revision ID: 0001_init
Revises: 
Create Date: 2026-02-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "competitors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("primary_domain", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "source_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False, server_default="high"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("raw_content", sa.Text()),
        sa.Column("raw_hash", sa.String(length=128)),
        sa.Column("structured_json", sa.JSON()),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text()),
        sa.Column("evidence_json", sa.JSON()),
        sa.Column("occurred_at", sa.DateTime()),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("snapshots")
    op.drop_table("source_endpoints")
    op.drop_table("competitors")
