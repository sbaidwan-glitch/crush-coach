"""answer_request and task_suggestion: what waits on a lead besides work to check

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-15 12:00:00.000000

`answer_request` is an "Answer to settle" (a member asked a lead to settle a coach
answer); `task_suggestion` is a "New task" a member suggested. Decisions keep their
names (proposed / confirmed / superseded); only the words on the pages change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STR = sqlmodel.sql.sqltypes.AutoString()


def upgrade() -> None:
    op.create_table(
        "answer_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("task_code", STR, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("subsystem", STR, nullable=False),
        sa.Column("question", STR, nullable=False),
        sa.Column("asked_by", STR, nullable=False),
        sa.Column("kind", STR, nullable=False),
        sa.Column("answer_text", STR, nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("suggested", sa.Integer(), nullable=True),
        sa.Column("suggestion", STR, nullable=True),
        sa.Column("status", STR, nullable=False),
        sa.Column("deferred_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_by", STR, nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("decision_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.ForeignKeyConstraint(["decision_id"], ["decision.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_answer_request_task_id"), "answer_request", ["task_id"])
    op.create_index(
        op.f("ix_answer_request_message_id"), "answer_request", ["message_id"]
    )
    op.create_index(
        op.f("ix_answer_request_subsystem"), "answer_request", ["subsystem"]
    )
    op.create_index(op.f("ix_answer_request_status"), "answer_request", ["status"])
    op.create_index(
        op.f("ix_answer_request_created_at"), "answer_request", ["created_at"]
    )
    op.create_table(
        "task_suggestion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", STR, nullable=False),
        sa.Column("why", STR, nullable=False),
        sa.Column("suggested_by", STR, nullable=False),
        sa.Column("status", STR, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_by", STR, nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("task_code", STR, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_suggestion_status"), "task_suggestion", ["status"])
    op.create_index(
        op.f("ix_task_suggestion_created_at"), "task_suggestion", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_task_suggestion_created_at"), table_name="task_suggestion")
    op.drop_index(op.f("ix_task_suggestion_status"), table_name="task_suggestion")
    op.drop_table("task_suggestion")
    op.drop_index(op.f("ix_answer_request_created_at"), table_name="answer_request")
    op.drop_index(op.f("ix_answer_request_status"), table_name="answer_request")
    op.drop_index(op.f("ix_answer_request_subsystem"), table_name="answer_request")
    op.drop_index(op.f("ix_answer_request_message_id"), table_name="answer_request")
    op.drop_index(op.f("ix_answer_request_task_id"), table_name="answer_request")
    op.drop_table("answer_request")
