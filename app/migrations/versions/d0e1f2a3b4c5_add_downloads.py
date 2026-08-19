"""Add downloads table

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4

The downloader feature tracks each update/DLC it has asked qBittorrent to fetch:
one row per (app_id, app_version) target, holding the torrent it picked and the
lifecycle state of that download. Completion is derived from app ownership — the
row flips to `completed` when the library watcher has identified the file — so
nothing here duplicates the Apps table.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd0e1f2a3b4c5'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('downloads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title_id', sa.String(), nullable=True),
        sa.Column('app_id', sa.String(), nullable=True),
        sa.Column('app_version', sa.String(), nullable=True),
        sa.Column('app_type', sa.String(), nullable=True),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('search_query', sa.String(), nullable=True),
        sa.Column('torrent_hash', sa.String(), nullable=True),
        sa.Column('torrent_name', sa.String(), nullable=True),
        sa.Column('indexer', sa.String(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('seeders', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=True, server_default='queued'),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('app_id', 'app_version', name='uq_downloads_app_version'),
    )
    op.create_index('ix_downloads_title_id', 'downloads', ['title_id'])
    op.create_index('ix_downloads_app_id', 'downloads', ['app_id'])
    op.create_index('ix_downloads_torrent_hash', 'downloads', ['torrent_hash'])


def downgrade():
    op.drop_index('ix_downloads_torrent_hash', table_name='downloads')
    op.drop_index('ix_downloads_app_id', table_name='downloads')
    op.drop_index('ix_downloads_title_id', table_name='downloads')
    op.drop_table('downloads')
