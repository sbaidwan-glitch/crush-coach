"""task_event table (task history); map older task status words to the new flow

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11 12:00:00.000000

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

# The task flow is now todo / in_progress / needs_review / done. Copied, not
# imported: a migration must keep meaning what it meant when written. Anything
# written by hand or imported with another word is mapped to the nearest status.
STATUS_MAP = {
    "not started": "todo",
    "not_started": "todo",
    "open": "todo",
    "blocked": "in_progress",
    "doing": "in_progress",
    "started": "in_progress",
    "in progress": "in_progress",
    "review": "needs_review",
    "in_review": "needs_review",
    "in review": "needs_review",
    "needs review": "needs_review",
    "complete": "done",
    "completed": "done",
    "finished": "done",
}


def upgrade() -> None:
    op.create_table(
        "task_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("actor", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("from_status", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("to_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_event_task_id", "task_event", ["task_id"])
    op.create_index("ix_task_event_created_at", "task_event", ["created_at"])

    task = sa.table("task", sa.column("status", sa.String()))
    for old, new in STATUS_MAP.items():
        op.execute(
            task.update()
            .where(sa.func.lower(sa.func.trim(task.c.status)) == old)
            .values(status=new)
        )
    # Whatever is left that the flow doesn't know starts over as to do.
    op.execute(
        task.update()
        .where(task.c.status.not_in(["todo", "in_progress", "needs_review", "done"]))
        .values(status="todo")
    )


def downgrade() -> None:
    # needs_review did not exist before: it was work still in progress.
    task = sa.table("task", sa.column("status", sa.String()))
    op.execute(
        task.update().where(task.c.status == "needs_review").values(status="in_progress")
    )
    op.drop_index("ix_task_event_created_at", table_name="task_event")
    op.drop_index("ix_task_event_task_id", table_name="task_event")
    op.drop_table("task_event")
