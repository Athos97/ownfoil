"""Add app blacklist and activity events

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6

Two user-data tables:

- app_blacklist: app ids deliberately excluded from "what's missing" (typically
  language-pack DLCs). Read by the title flag recompute and the downloader's
  target selection, both inside their own transactions, which is why it is a
  table rather than settings.yaml.
- activity_events: who connected to the shop, who downloaded what, and web
  logins - the data behind the admin Activity page. Pruned by the writer to the
  newest rows, so no cleanup migration is ever needed.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('app_blacklist',
        sa.Column('app_id', sa.String(), nullable=False),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('app_id'),
    )
    op.create_table('activity_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=True),
        sa.Column('kind', sa.String(), nullable=True),
        sa.Column('username', sa.String(), nullable=True),
        sa.Column('client', sa.String(), nullable=True),
        sa.Column('device_uid', sa.String(), nullable=True),
        sa.Column('ip', sa.String(), nullable=True),
        sa.Column('filename', sa.String(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('detail', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_activity_events_ts', 'activity_events', ['ts'])


def downgrade():
    op.drop_index('ix_activity_events_ts', table_name='activity_events')
    op.drop_table('activity_events')
    op.drop_table('app_blacklist')
