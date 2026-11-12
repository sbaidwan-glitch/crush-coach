"""task_removal: a log of who removed which task

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_removal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=False),
        sa.Column("removed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("task_code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("task_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("coach_messages_deleted", sa.Integer(), nullable=False),
        sa.Column("decision_links_deleted", sa.Integer(), nullable=False),
        sa.Column("llm_calls_unlinked", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_task_removal_batch"), "task_removal", ["batch"])
    op.create_index(
        op.f("ix_task_removal_removed_at"), "task_removal", ["removed_at"]
    )
    op.create_index(op.f("ix_task_removal_task_code"), "task_removal", ["task_code"])


def downgrade() -> None:
    op.drop_index(op.f("ix_task_removal_task_code"), table_name="task_removal")
    op.drop_index(op.f("ix_task_removal_removed_at"), table_name="task_removal")
    op.drop_index(op.f("ix_task_removal_batch"), table_name="task_removal")
    op.drop_table("task_removal")
