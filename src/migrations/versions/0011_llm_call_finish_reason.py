"""llm_call: finish reason, reasoning tokens, whether the tool was called

Says WHY an answer was cut off or came back empty (output cap, hidden thinking
tokens, a tool request with no text), instead of guessing.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-12 12:00:00.000000

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


def upgrade() -> None:
    with op.batch_alter_table("llm_call") as batch:
        batch.add_column(
            sa.Column(
                "finish_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True
            )
        )
        batch.add_column(sa.Column("reasoning_tokens", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "tool_called",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_call") as batch:
        batch.drop_column("tool_called")
        batch.drop_column("reasoning_tokens")
        batch.drop_column("finish_reason")
