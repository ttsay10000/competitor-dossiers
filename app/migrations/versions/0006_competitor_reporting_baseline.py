"""add competitors.reporting_baseline_at for seed/baseline reporting

Revision ID: 0006_competitor_reporting_baseline
Revises: 0005_extra_options
Create Date: 2026-02-10 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_competitor_reporting_baseline"
down_revision = "0005_extra_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column("reporting_baseline_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competitors", "reporting_baseline_at")
