"""Add indexes on files.library_id and apps.title_id

Revision ID: d1e2f3a4b5c6
Revises: c5d6e7f8a9b0

Neither foreign key had a supporting index (SQLite does not create one
automatically for a FK column), so cascade-delete lookups by library_id
(get_library_file_paths) and title->apps traversal (get_all_title_apps,
Titles.apps backref) fell back to a full scan of files/apps. Same class of
gap f6a7b8c9d0e1 already fixed for app_files.file_id.
"""
from alembic import op


revision = 'd1e2f3a4b5c6'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_files_library_id', 'files', ['library_id'])
    op.create_index('ix_apps_title_id', 'apps', ['title_id'])


def downgrade():
    op.drop_index('ix_apps_title_id', table_name='apps')
    op.drop_index('ix_files_library_id', table_name='files')
