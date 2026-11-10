"""create decision and decision_task tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09 16:52:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subsystem", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("topic", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("decision", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("rationale", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("alternatives", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
        sa.Column("proposed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("drafted_by_coach", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["decision.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_decision_status"), "decision", ["status"], unique=False)
    op.create_index(
        op.f("ix_decision_subsystem"), "decision", ["subsystem"], unique=False
    )
    op.create_table(
        "decision_task",
        sa.Column("decision_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["decision.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("decision_id", "task_id"),
    )
    op.create_index(
        op.f("ix_decision_task_task_id"), "decision_task", ["task_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_decision_task_task_id"), table_name="decision_task")
    op.drop_table("decision_task")
    op.drop_index(op.f("ix_decision_subsystem"), table_name="decision")
    op.drop_index(op.f("ix_decision_status"), table_name="decision")
    op.drop_table("decision")
