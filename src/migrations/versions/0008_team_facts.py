"""create team_fact table, with the three starting facts

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11 09:00:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Starting content only; leads edit it at /admin/facts. Made-up-safe: nothing here
# names a person or quotes a private document.
STARTING_FACTS = (
    "Current season is 2026-27",
    "Crush is the current vehicle; Alfie docs are older and may be stale",
    "Current Crush CAD lives in the 2025-2026 Mechanical CAD folder",
)


def upgrade() -> None:
    table = op.create_table(
        "team_fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    op.bulk_insert(
        table,
        [
            {"text": text, "updated_by": None, "updated_at": now}
            for text in STARTING_FACTS
        ],
    )


def downgrade() -> None:
    op.drop_table("team_fact")
