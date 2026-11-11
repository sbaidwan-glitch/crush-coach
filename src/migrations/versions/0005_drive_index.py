"""create the Drive index tables: drive_file, drive_chunk, sync_state

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in step with models.CHUNK_SEARCH_VECTOR_SQL. Copied, not imported: a
# migration must keep meaning what it meant when it was written.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(heading, '')), 'A') || "
    "setweight(to_tsvector('english', text), 'C')"
)


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"

    op.create_table(
        "drive_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("drive_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("mime_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("parent_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("folder_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("modified_time", sa.DateTime(), nullable=True),
        sa.Column("web_link", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("text_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("text_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_drive_file_drive_id"), "drive_file", ["drive_id"], unique=True
    )
    op.create_index(op.f("ix_drive_file_kind"), "drive_file", ["kind"], unique=False)

    # Postgres generates the full-text column itself; SQLite (tests) has no
    # to_tsvector, so there it is an empty text column and search uses LIKE.
    if is_postgres:
        search_vector = sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        )
    else:
        search_vector = sa.Column("search_vector", sa.Text(), nullable=True)
    op.create_table(
        "drive_chunk",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("heading", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        search_vector,
        sa.ForeignKeyConstraint(["file_id"], ["drive_file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_drive_chunk_file_id"), "drive_chunk", ["file_id"], unique=False
    )
    op.create_index(
        "ix_drive_chunk_search_vector",
        "drive_chunk",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )

    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("page_token", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_full_sync", sa.DateTime(), nullable=True),
        sa.Column("last_incremental_sync", sa.DateTime(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("running_since", sa.DateTime(), nullable=True),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    op.drop_index("ix_drive_chunk_search_vector", table_name="drive_chunk")
    op.drop_index(op.f("ix_drive_chunk_file_id"), table_name="drive_chunk")
    op.drop_table("drive_chunk")
    op.drop_index(op.f("ix_drive_file_kind"), table_name="drive_file")
    op.drop_index(op.f("ix_drive_file_drive_id"), table_name="drive_file")
    op.drop_table("drive_file")
