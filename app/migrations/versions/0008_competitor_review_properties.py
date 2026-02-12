"""add competitor_review_properties for Google Reviews tracking

Revision ID: 0008_review_properties
Revises: 0007_is_active
Create Date: 2026-02-12 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_review_properties"
down_revision = "0007_is_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "competitor_review_properties",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("place_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_competitor_review_properties_competitor_id",
        "competitor_review_properties",
        ["competitor_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_competitor_review_properties_competitor_id", table_name="competitor_review_properties")
    op.drop_table("competitor_review_properties")
