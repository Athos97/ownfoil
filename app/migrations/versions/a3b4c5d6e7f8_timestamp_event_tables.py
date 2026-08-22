"""Timestamp ignored_events and temp_files

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7

Both tables only ever grew: an ignored event whose filesystem callback never
arrived (watcher off, skipped poll) would swallow one future real delete of
the same path, and a temp-file claim a crashed task never released blocked
scan/organize for that path until a restart. created_at columns plus TTL
purges (run at startup and by the downloader passes) bound both.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3b4c5d6e7f8'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ignored_events',
                  sa.Column('created_at', sa.DateTime(), nullable=True))
    op.add_column('temp_files',
                  sa.Column('created_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('temp_files', 'created_at')
    op.drop_column('ignored_events', 'created_at')
