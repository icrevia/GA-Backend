"""Add prize_distribution to tournaments

Revision ID: f1a2b3c4d5e6
Revises: db910f5991e5
Create Date: 2026-04-24 01:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'db910f5991e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('tournaments', sa.Column('prize_distribution', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tournaments', 'prize_distribution')
