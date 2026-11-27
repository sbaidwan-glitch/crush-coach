"""notebook: entry states, photos, suggested specs, private notes, llm_call purpose

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-26 12:00:00.000000

The work log becomes the Notebook.

- `work_note.state` is `draft` (only its member sees it) or `published`; every note that
  exists now becomes published. `refers_to_id` is the earlier entry a new one adds to (a
  published entry is never edited). `updated_at` is when a draft was last saved.
  `specs_dropped` counts suggested specs thrown away for having no source.
- `note_photo`: up to six photos per entry, stored here as re-encoded JPEGs.
- `note_spec`: each spec the model suggested for an entry, and whether the member kept it
  (accepted), changed it (edited) or not.
- `private_note`: a member's private notes. No task, no Drive write, no model.
- `llm_call.purpose`: what each model call was for, so the cost of tidying, suggesting specs
  and planning can be told apart from coach answers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STR = sqlmodel.sql.sqltypes.AutoString()


def upgrade() -> None:
    with op.batch_alter_table("work_note") as batch:
        batch.add_column(
            sa.Column("state", STR, nullable=False, server_default="published")
        )
        batch.add_column(sa.Column("refers_to_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("specs_dropped", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_work_note_refers_to_id", "work_note", ["refers_to_id"], ["id"]
        )
    op.execute("UPDATE work_note SET updated_at = posted_at")
    with op.batch_alter_table("work_note") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)
    op.create_index(op.f("ix_work_note_state"), "work_note", ["state"])
    op.create_index(op.f("ix_work_note_refers_to_id"), "work_note", ["refers_to_id"])

    op.create_table(
        "note_photo",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=False),
        sa.Column("member", STR, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content_type", STR, nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("drive_file_id", STR, nullable=True),
        sa.Column("drive_link", STR, nullable=True),
        sa.ForeignKeyConstraint(["note_id"], ["work_note.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_note_photo_note_id"), "note_photo", ["note_id"])
    op.create_index(op.f("ix_note_photo_member"), "note_photo", ["member"])

    op.create_table(
        "note_spec",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", STR, nullable=False),
        sa.Column("original_text", STR, nullable=False),
        sa.Column("unit", STR, nullable=False),
        sa.Column("source_title", STR, nullable=False),
        sa.Column("source_link", STR, nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("edited", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["work_note.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_note_spec_note_id"), "note_spec", ["note_id"])

    op.create_table(
        "private_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member", STR, nullable=False),
        sa.Column("text", STR, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_private_note_member"), "private_note", ["member"])
    op.create_index(op.f("ix_private_note_created_at"), "private_note", ["created_at"])

    with op.batch_alter_table("llm_call") as batch:
        batch.add_column(sa.Column("purpose", STR, nullable=True))
    op.create_index(op.f("ix_llm_call_purpose"), "llm_call", ["purpose"])


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_call_purpose"), table_name="llm_call")
    with op.batch_alter_table("llm_call") as batch:
        batch.drop_column("purpose")

    op.drop_index(op.f("ix_private_note_created_at"), table_name="private_note")
    op.drop_index(op.f("ix_private_note_member"), table_name="private_note")
    op.drop_table("private_note")

    op.drop_index(op.f("ix_note_spec_note_id"), table_name="note_spec")
    op.drop_table("note_spec")

    op.drop_index(op.f("ix_note_photo_member"), table_name="note_photo")
    op.drop_index(op.f("ix_note_photo_note_id"), table_name="note_photo")
    op.drop_table("note_photo")

    op.drop_index(op.f("ix_work_note_refers_to_id"), table_name="work_note")
    op.drop_index(op.f("ix_work_note_state"), table_name="work_note")
    with op.batch_alter_table("work_note") as batch:
        batch.drop_constraint("fk_work_note_refers_to_id", type_="foreignkey")
        batch.drop_column("updated_at")
        batch.drop_column("specs_dropped")
        batch.drop_column("refers_to_id")
        batch.drop_column("state")
