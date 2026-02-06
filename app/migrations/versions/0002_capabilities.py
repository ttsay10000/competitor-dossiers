"""add capabilities table

Revision ID: 0002_capabilities
Revises: 0001_init
Create Date: 2026-02-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_capabilities"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id"), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("capabilities")
