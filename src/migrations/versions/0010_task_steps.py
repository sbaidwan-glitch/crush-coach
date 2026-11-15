"""task steps: per-task step lists, richer history, Print and Investigate types

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-13 10:00:00.000000

Task types are plain text, so Print and Investigate need no schema change. What this adds:
`task.steps` (a lead's own step list; NULL = the type's defaults), and `task_event.kind` /
`detail` (so the History can say "finished Drawing", "self-approved", "changed the type").

It also makes every task's stage agree with its status under the new step lists (the
"AI pre-check" step is gone): a task waiting for review is on its review step, a task in
progress or not started is on a working step, a done task is on the last step.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied from steps.py, not imported: a migration keeps meaning what it meant when
# written. type -> ((step name, is a review step), ...)
STEPS = {
    "Fabricate": (
        ("Drawing", False),
        ("Lead review", True),
        ("Process router", False),
        ("Shop", False),
        ("Inspect", False),
    ),
    "Design": (
        ("Requirements", False),
        ("Concepts", False),
        ("Lead review", True),
        ("CAD", False),
        ("Fit check", False),
    ),
    "Validate": (
        ("Test plan", False),
        ("Lead approval", True),
        ("Run", False),
        ("Log results", False),
    ),
}
FALLBACK = (("Work", False), ("Lead review", True))
LEGACY = {"ai pre-check": "drawing", "calculations": "test plan"}


def fitted_stage(task_type: str, status: str, stage: str) -> str:
    """The stage that agrees with the status, starting from the one stored."""
    steps = STEPS.get(task_type, FALLBACK)
    names = [name.casefold() for name, _ in steps]
    word = " ".join((stage or "").split()).casefold()
    word = LEGACY.get(word, word)
    index = names.index(word) if word in names else None
    if index is None:
        index = next((i for i, n in enumerate(names) if word.startswith(n)), 0)
    if status == "done":
        return steps[-1][0]
    if status == "needs_review":
        reviews = [i for i, (_, review) in enumerate(steps) if review]
        index = next((i for i in reviews if i >= index), reviews[-1])
        return steps[index][0]
    while index > 0 and steps[index][1]:
        index -= 1
    return steps[index][0]


def upgrade() -> None:
    op.add_column("task", sa.Column("steps", sa.JSON(none_as_null=True), nullable=True))
    op.add_column(
        "task_event", sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
    )
    op.add_column(
        "task_event",
        sa.Column("detail", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )

    task = sa.table(
        "task",
        sa.column("id", sa.Integer()),
        sa.column("task_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("stage", sa.String()),
    )
    bind = op.get_bind()
    for row in bind.execute(
        sa.select(task.c.id, task.c.task_type, task.c.status, task.c.stage)
    ).all():
        stage = fitted_stage(row.task_type, row.status, row.stage)
        if stage != row.stage:
            bind.execute(task.update().where(task.c.id == row.id).values(stage=stage))


def downgrade() -> None:
    # The stages written above are still valid step names for the old lists, except
    # the steps that were removed; they simply read as the nearest earlier step.
    op.drop_column("task_event", "detail")
    op.drop_column("task_event", "kind")
    op.drop_column("task", "steps")
