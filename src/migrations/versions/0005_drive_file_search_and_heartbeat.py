"""full-text entry for every Drive file; sync heartbeat

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported: a migration must keep meaning what it meant when written.
# Kept in step with models.FILE_SEARCH_VECTOR_SQL.
FILE_SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', regexp_replace(name, '[_./-]+', ' ', 'g')), 'A') || "
    "setweight(to_tsvector('english', "
    "regexp_replace(folder_path, '[_./-]+', ' ', 'g')), 'C') || "
    "setweight(to_tsvector('english', CASE kind "
    "WHEN 'cad' THEN 'cad model' WHEN 'doc' THEN 'doc document' "
    "WHEN 'sheet' THEN 'sheet spreadsheet' WHEN 'pdf' THEN 'pdf' "
    "WHEN 'folder' THEN 'folder' ELSE '' END), 'D')"
)


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"

    # Postgres generates the column (and fills it for existing rows); SQLite
    # (tests) has no to_tsvector, so there it is an empty text column.
    if is_postgres:
        search_vector = sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(FILE_SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        )
    else:
        search_vector = sa.Column("search_vector", sa.Text(), nullable=True)
    op.add_column("drive_file", search_vector)
    op.create_index(
        "ix_drive_file_search_vector",
        "drive_file",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.add_column("sync_state", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Batch mode so SQLite (tests) can drop columns too.
    with op.batch_alter_table("sync_state") as batch:
        batch.drop_column("heartbeat_at")
    op.drop_index("ix_drive_file_search_vector", table_name="drive_file")
    with op.batch_alter_table("drive_file") as batch:
        batch.drop_column("search_vector")
