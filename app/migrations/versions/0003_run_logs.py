"""add run_logs table

Revision ID: 0003_run_logs
Revises: 0002_capabilities
Create Date: 2026-02-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_run_logs"
down_revision = "0002_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id")),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("extra_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("run_logs")
