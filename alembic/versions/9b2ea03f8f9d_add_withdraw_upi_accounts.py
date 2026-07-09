"""add withdraw upi accounts

Revision ID: 9b2ea03f8f9d
Revises: 3fae9d91b3c0, 7f927624aa9f
Create Date: 2026-04-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b2ea03f8f9d"
down_revision: Union[str, Sequence[str], None] = ("3fae9d91b3c0", "7f927624aa9f")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "withdraw_upi_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_holder_name", sa.String(length=120), nullable=False),
        sa.Column("upi_id", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "upi_id", name="uq_withdraw_upi_accounts_user_upi"),
    )
    op.create_index(op.f("ix_withdraw_upi_accounts_id"), "withdraw_upi_accounts", ["id"], unique=False)
    op.create_index(op.f("ix_withdraw_upi_accounts_user_id"), "withdraw_upi_accounts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_withdraw_upi_accounts_user_id"), table_name="withdraw_upi_accounts")
    op.drop_index(op.f("ix_withdraw_upi_accounts_id"), table_name="withdraw_upi_accounts")
    op.drop_table("withdraw_upi_accounts")
