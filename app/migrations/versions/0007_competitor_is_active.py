"""add competitors.is_active for cron/global refresh exclusion

Revision ID: 0007_is_active
Revises: 0006_reporting_baseline
Create Date: 2026-02-12 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_is_active"
down_revision = "0006_reporting_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("competitors", "is_active")
