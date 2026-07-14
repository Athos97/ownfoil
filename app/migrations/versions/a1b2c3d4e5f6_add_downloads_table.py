"""Add downloads table

Revision ID: a1b2c3d4e5f6
Revises: 78c33e9bffce

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = '78c33e9bffce'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'downloads' not in inspector.get_table_names():
        op.create_table('downloads',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('title_id', sa.String(), nullable=True),
            sa.Column('app_id', sa.String(), nullable=True),
            sa.Column('app_version', sa.String(), nullable=True),
            sa.Column('app_type', sa.String(), nullable=True),
            sa.Column('search_query', sa.String(), nullable=True),
            sa.Column('torrent_hash', sa.String(), nullable=True),
            sa.Column('torrent_name', sa.String(), nullable=True),
            sa.Column('indexer', sa.String(), nullable=True),
            sa.Column('size', sa.Integer(), nullable=True),
            sa.Column('seeders', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(), nullable=True),
            sa.Column('error', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('app_id', 'app_version', name='uq_downloads_app_version'),
        )
        with op.batch_alter_table('downloads') as batch_op:
            batch_op.create_index('ix_downloads_title_id', ['title_id'])
            batch_op.create_index('ix_downloads_app_id', ['app_id'])
            batch_op.create_index('ix_downloads_torrent_hash', ['torrent_hash'])
        with op.batch_alter_table('downloads') as batch_op:
            batch_op.alter_column('status', server_default='queued')


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'downloads' in inspector.get_table_names():
        op.drop_table('downloads')
