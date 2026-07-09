"""add bio to users

Revision ID: c4a9bd4f2191
Revises: 9b2ea03f8f9d
Create Date: 2026-04-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4a9bd4f2191"
down_revision: Union[str, Sequence[str], None] = "9b2ea03f8f9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("bio", sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "bio")
