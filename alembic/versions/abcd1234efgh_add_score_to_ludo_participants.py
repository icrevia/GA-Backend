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
    from core.database import Base
    import models.user
    import models.tournament
    import models.wallet
    import models.participant
    import models.support
    import models.ludo
    import models.quiz
    import models.admin_access_session
    import models.banner
    import models.config
    import models.daily_stats
    import models.notification
    import models.otp_phone_lock
    import models.pending_otp
    import models.promo
    import models.restriction
    import models.user_activity_lock
    import models.withdraw_upi_account
    
    conn = op.get_bind()
    Base.metadata.create_all(conn)

    # Add score column to ludo_participants table if it doesn't exist
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('ludo_participants')]
    if 'score' not in columns:
        op.add_column('ludo_participants', sa.Column('score', sa.Integer(), nullable=True))


def downgrade() -> None:
    # Remove score column from ludo_participants table
    op.drop_column('ludo_participants', 'score')
