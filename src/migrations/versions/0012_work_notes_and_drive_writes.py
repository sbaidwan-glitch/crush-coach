"""work_note and drive_write: the work log and what is written to the Crush document

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17 12:00:00.000000

`work_note` is one entry in a member's notebook (what they did on a task and why, plus
their own words from before "Tidy my note"). `drive_write` records the one write of an
entry (a note or a recorded answer) to the Crush document, so a write is never repeated
and a failed one can be retried.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STR = sqlmodel.sql.sqltypes.AutoString()


def upgrade() -> None:
    op.create_table(
        "work_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member", STR, nullable=False),
        sa.Column("member_name", STR, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("task_code", STR, nullable=False),
        sa.Column("subsystem", STR, nullable=False),
        sa.Column("did", STR, nullable=False),
        sa.Column("why", STR, nullable=False),
        sa.Column("original", STR, nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_work_note_member"), "work_note", ["member"])
    op.create_index(op.f("ix_work_note_task_id"), "work_note", ["task_id"])
    op.create_index(op.f("ix_work_note_subsystem"), "work_note", ["subsystem"])
    op.create_index(op.f("ix_work_note_posted_at"), "work_note", ["posted_at"])
    op.create_table(
        "drive_write",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", STR, nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("status", STR, nullable=False),
        sa.Column("tab_title", STR, nullable=True),
        sa.Column("tab_id", STR, nullable=True),
        sa.Column("used_general", sa.Boolean(), nullable=False),
        sa.Column("error", STR, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("written_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "ref_id", name="uq_drive_write_entry"),
    )
    op.create_index(op.f("ix_drive_write_status"), "drive_write", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_drive_write_status"), table_name="drive_write")
    op.drop_table("drive_write")
    op.drop_index(op.f("ix_work_note_posted_at"), table_name="work_note")
    op.drop_index(op.f("ix_work_note_subsystem"), table_name="work_note")
    op.drop_index(op.f("ix_work_note_task_id"), table_name="work_note")
    op.drop_index(op.f("ix_work_note_member"), table_name="work_note")
    op.drop_table("work_note")
