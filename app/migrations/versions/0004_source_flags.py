"""add source endpoint flags

Revision ID: 0004_source_flags
Revises: 0003_run_logs
Create Date: 2026-02-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_source_flags"
down_revision = "0003_run_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_endpoints", sa.Column("js_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("source_endpoints", sa.Column("use_sitemap_first", sa.Boolean(), server_default=sa.text("false"), nullable=False))


def downgrade() -> None:
    op.drop_column("source_endpoints", "use_sitemap_first")
    op.drop_column("source_endpoints", "js_required")
