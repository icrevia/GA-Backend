"""add team_name, team_join_code, is_team_captain to tournament_participants

Revision ID: a1b2c3d4e5f6
Revises: 3fae9d91b3c0
Create Date: 2026-04-11 22:36:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "3fae9d91b3c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tournament_participants") as batch_op:
        batch_op.add_column(sa.Column("team_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("team_join_code", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("is_team_captain", sa.Boolean(), nullable=True, server_default=sa.text("0")))
        batch_op.create_index("ix_tp_team_join_code", ["team_join_code"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tournament_participants") as batch_op:
        batch_op.drop_index("ix_tp_team_join_code")
        batch_op.drop_column("is_team_captain")
        batch_op.drop_column("team_join_code")
        batch_op.drop_column("team_name")
