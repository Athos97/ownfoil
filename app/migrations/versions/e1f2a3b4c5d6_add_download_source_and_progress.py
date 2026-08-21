"""Add source and progress to downloads

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5

Downloads now come from more than one source (torrents via Jackett/qBittorrent,
direct HTTP from Ghost eShop). `source` records which lane a row belongs to so
each source only retries its own failures, and `progress` carries a 0-100
percentage for rows whose transfer ownfoil drives itself (Ghost eShop chunked
downloads; mirrored from qBittorrent's own progress for torrents).
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('downloads', sa.Column('source', sa.String(), nullable=True))
    op.add_column('downloads', sa.Column('progress', sa.Integer(), nullable=True))
    # Rows written before the split are all Jackett/qBittorrent lanes.
    op.execute("UPDATE downloads SET source = 'torrents' WHERE source IS NULL")


def downgrade():
    op.drop_column('downloads', 'progress')
    op.drop_column('downloads', 'source')
