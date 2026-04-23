"""Merge performance indexes and prize distribution heads

Revision ID: a9b8c7d6e5f4
Revises: 8010dcb382ed, f1a2b3c4d5e6
Create Date: 2026-04-24 01:53:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, Sequence[str], None] = ('8010dcb382ed', 'f1a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge heads — no schema changes needed."""
    pass


def downgrade() -> None:
    """Merge heads — no schema changes needed."""
    pass
