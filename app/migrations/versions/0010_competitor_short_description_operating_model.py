"""Add short_description and operating_model_description to competitors

Revision ID: 0010_short_desc_operating
Revises: 0009_created_at_three
Create Date: 2026-02-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0010_short_desc_operating"
down_revision = "0009_created_at_three"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column("short_description", sa.Text(), nullable=True),
    )
    op.add_column(
        "competitors",
        sa.Column("operating_model_description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competitors", "operating_model_description")
    op.drop_column("competitors", "short_description")
