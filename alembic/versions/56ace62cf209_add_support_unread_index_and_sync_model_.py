"""add support unread index and sync model metadata

Revision ID: 56ace62cf209
Revises: 8010dcb382ed
Create Date: 2026-04-13 14:12:50.816481

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '56ace62cf209'
down_revision: Union[str, Sequence[str], None] = '8010dcb382ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add new optimized index for support dashboard
    op.create_index('ix_chat_messages_unread', 'chat_messages', ['session_id', 'is_admin', 'is_read'], unique=False)

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chat_messages_unread', table_name='chat_messages')
