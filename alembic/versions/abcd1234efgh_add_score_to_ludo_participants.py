"""add score to ludo_participants

Revision ID: abcd1234efgh
Revises: f1a2b3c4d5e6
Create Date: 2026-07-05 23:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'abcd1234efgh'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add score column to ludo_participants table
    op.add_column('ludo_participants', sa.Column('score', sa.Integer(), nullable=True))


def downgrade() -> None:
    # Remove score column from ludo_participants table
    op.drop_column('ludo_participants', 'score')
