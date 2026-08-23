"""Add dlcs_up_to_date to titles

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9

Splits the DLC status into two flags: complete (all owned) and
dlcs_up_to_date (all owned DLCs at their latest known version).
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('titles', sa.Column('dlcs_up_to_date', sa.Boolean(),
                                      server_default='1', nullable=False))


def downgrade():
    op.drop_column('titles', 'dlcs_up_to_date')
