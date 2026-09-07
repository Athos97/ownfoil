"""Add downloads.note

Revision ID: a1b2c3d4e5f6
Revises: d1e2f3a4b5c6

A completed row's `error` column doubles today as an informational message
for non-failure outcomes (a source resolving a target without transferring
anything, or landing something short of the newest version known) - but
that overloading meant the completion paths that flip a row to `completed`
via ownership (sync_downloads_status, complete_downloads_for_apps) always
null `error` out, wiping the very message that explained the outcome. A
dedicated, untouched `note` column lets that context survive to completion
and reach the UI, instead of `error` staying reserved for actual failures.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7c8d9e0f1a2'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('downloads', sa.Column('note', sa.String(), nullable=True))


def downgrade():
    op.drop_column('downloads', 'note')
