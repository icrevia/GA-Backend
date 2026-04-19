"""Add support audio duration

Revision ID: 7c2f9b1d4e6a
Revises: eeccdc942bc2
Create Date: 2026-04-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c2f9b1d4e6a'
down_revision: Union[str, Sequence[str], None] = 'eeccdc942bc2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chat_messages', sa.Column('media_duration_seconds', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chat_messages', 'media_duration_seconds')