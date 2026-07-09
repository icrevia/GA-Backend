"""add banner_url to quiz_matches

Revision ID: f1a2b3c4d5e6
Revises: 5e2d8c3fdbda
Create Date: 2026-05-12 18:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = '5e2d8c3fdbda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('quiz_matches')]
    if 'banner_url' not in columns:
        op.add_column('quiz_matches', sa.Column('banner_url', sa.String(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('quiz_matches')]
    if 'banner_url' in columns:
        op.drop_column('quiz_matches', 'banner_url')
