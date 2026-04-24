"""add rank kills and prize to participants

Revision ID: 5e2d8c3fdbda
Revises: 7c2f9b1d4e6a
Create Date: 2026-04-24 16:13:34.395676

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e2d8c3fdbda'
down_revision: Union[str, Sequence[str], None] = '7c2f9b1d4e6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tournament_participants', sa.Column('participant_rank', sa.Integer(), nullable=True))
    op.add_column('tournament_participants', sa.Column('kills', sa.Integer(), server_default='0', nullable=False))
    op.add_column('tournament_participants', sa.Column('prize_amount', sa.String(), nullable=True))

def downgrade() -> None:
    op.drop_column('tournament_participants', 'participant_rank')
    op.drop_column('tournament_participants', 'kills')
    op.drop_column('tournament_participants', 'prize_amount')
