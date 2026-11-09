"""add tracker text fields to task

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_COLUMNS = (
    "description",
    "dependencies",
    "exploratory_questions",
    "skills",
    "requirements",
    "people_to_ask",
    "documentation",
    "due_date",
)


def upgrade() -> None:
    for name in NEW_COLUMNS:
        op.add_column(
            "task",
            sa.Column(name, sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )


def downgrade() -> None:
    # Batch mode so SQLite (tests) can drop columns too.
    with op.batch_alter_table("task") as batch:
        for name in reversed(NEW_COLUMNS):
            batch.drop_column(name)
