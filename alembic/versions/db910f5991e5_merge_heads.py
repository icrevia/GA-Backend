"""Merge heads

Revision ID: db910f5991e5
Revises: a1b2c3d4e5f6, e91c2b5a7d31
Create Date: 2026-04-13 10:46:37.740766

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db910f5991e5'
down_revision: Union[str, Sequence[str], None] = ('a1b2c3d4e5f6', 'e91c2b5a7d31')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
