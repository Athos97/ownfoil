"""Add name column to downloads

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6

"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'downloads' in inspector.get_table_names():
        existing_cols = {c['name'] for c in inspector.get_columns('downloads')}
        if 'name' not in existing_cols:
            with op.batch_alter_table('downloads') as batch_op:
                batch_op.add_column(sa.Column('name', sa.String(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'downloads' in inspector.get_table_names():
        existing_cols = {c['name'] for c in inspector.get_columns('downloads')}
        if 'name' in existing_cols:
            with op.batch_alter_table('downloads') as batch_op:
                batch_op.drop_column('name')
