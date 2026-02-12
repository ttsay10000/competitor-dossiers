"""Set created_at to 2026-02-02 for Placemakr, Lark, AvantStay

Revision ID: 0009_created_at_three
Revises: 0008_review_properties
Create Date: 2026-02-12

"""
from alembic import op

revision = "0009_created_at_three"
down_revision = "0008_review_properties"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE competitors
        SET created_at = '2026-02-02 00:00:00'
        WHERE name IN ('Placemakr', 'Lark', 'AvantStay')
        """
    )


def downgrade() -> None:
    # Data migration: no reliable way to restore previous created_at values
    pass
