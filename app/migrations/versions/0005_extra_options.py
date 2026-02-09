"""add source_endpoints.extra_options for strategy/config

Revision ID: 0005_extra_options
Revises: 0004_source_flags
Create Date: 2026-02-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_extra_options"
down_revision = "0004_source_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_endpoints", sa.Column("extra_options", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_endpoints", "extra_options")
