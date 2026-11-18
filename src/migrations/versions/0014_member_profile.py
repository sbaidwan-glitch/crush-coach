"""member_profile: the icon and short name a member picks at /profile

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17 18:00:00.000000

One row per member who picked, keyed by email. `full_name` is the Google name they were
signed in with, which is what a task's `owner` holds, so a task can find its owner's icon.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STR = sqlmodel.sql.sqltypes.AutoString()


def upgrade() -> None:
    op.create_table(
        "member_profile",
        sa.Column("email", STR, nullable=False),
        sa.Column("icon", STR, nullable=False),
        sa.Column("display_name", STR, nullable=False),
        sa.Column("full_name", STR, nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_index(op.f("ix_member_profile_full_name"), "member_profile", ["full_name"])


def downgrade() -> None:
    op.drop_index(op.f("ix_member_profile_full_name"), table_name="member_profile")
    op.drop_table("member_profile")
