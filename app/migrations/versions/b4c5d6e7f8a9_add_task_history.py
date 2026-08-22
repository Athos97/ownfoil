"""Add task history

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8

Completed tasks are deleted from `tasks` (the live page only shows in-flight
work), which erased every trace of what ran. This table keeps the terminal
outcome of each task - success, failure, cancellation - written at the same
transitions that delete the live row, pruned to the newest 100.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4c5d6e7f8a9'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('task_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=True),
        sa.Column('task_name', sa.String(), nullable=True),
        sa.Column('display_name', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_history_completed_at', 'task_history', ['completed_at'])


def downgrade():
    op.drop_index('ix_task_history_completed_at', table_name='task_history')
    op.drop_table('task_history')
