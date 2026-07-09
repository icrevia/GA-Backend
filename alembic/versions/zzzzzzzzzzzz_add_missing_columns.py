"""Add all missing columns to users, tournaments, wallet_transactions, and tournament_participants

Revision ID: zzzzzzzzzzzz
Revises: abcd1234efgh
Create Date: 2026-07-09 20:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# revision identifiers, used by Alembic.
revision: str = 'zzzzzzzzzzzz'
down_revision: Union[str, Sequence[str], None] = 'abcd1234efgh'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)

    # Helper function to add columns safely
    def add_column_if_not_exists(table_name, column_name, column):
        columns = [col['name'] for col in inspector.get_columns(table_name)]
        if column_name not in columns:
            op.add_column(table_name, column)
            print(f"Added column {column_name} to {table_name}")

    # users
    add_column_if_not_exists("users", "mmr", sa.Column("mmr", sa.Integer(), nullable=False, server_default="1200"))
    add_column_if_not_exists("users", "xp", sa.Column("xp", sa.Integer(), nullable=False, server_default="0"))
    add_column_if_not_exists("users", "bonus_balance", sa.Column("bonus_balance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"))
    add_column_if_not_exists("users", "token_version", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    add_column_if_not_exists("users", "daily_spin_limit", sa.Column("daily_spin_limit", sa.Integer(), nullable=False, server_default="1"))
    add_column_if_not_exists("users", "daily_bonus_cycle_key", sa.Column("daily_bonus_cycle_key", sa.String(16), nullable=True))
    add_column_if_not_exists("users", "last_login_device", sa.Column("last_login_device", sa.String(160), nullable=True))
    add_column_if_not_exists("users", "fcm_token", sa.Column("fcm_token", sa.String(512), nullable=True))
    add_column_if_not_exists("users", "referral_code", sa.Column("referral_code", sa.String(), nullable=True))
    add_column_if_not_exists("users", "referred_by_id", sa.Column("referred_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    add_column_if_not_exists("users", "last_login_ip", sa.Column("last_login_ip", sa.String(64), nullable=True))
    add_column_if_not_exists("users", "daily_bonus_used", sa.Column("daily_bonus_used", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"))
    add_column_if_not_exists("users", "level", sa.Column("level", sa.Integer(), nullable=False, server_default="1"))
    add_column_if_not_exists("users", "daily_spin_cycle_key", sa.Column("daily_spin_cycle_key", sa.String(16), nullable=True))
    add_column_if_not_exists("users", "daily_spin_used", sa.Column("daily_spin_used", sa.Integer(), nullable=False, server_default="0"))
    add_column_if_not_exists("users", "winning_balance", sa.Column("winning_balance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"))
    add_column_if_not_exists("users", "phone_number", sa.Column("phone_number", sa.String(), nullable=True))
    add_column_if_not_exists("users", "profile_pic", sa.Column("profile_pic", sa.String(), nullable=True))
    add_column_if_not_exists("users", "last_login_at", sa.Column("last_login_at", sa.DateTime(), nullable=True))
    add_column_if_not_exists("users", "admin_permissions", sa.Column("admin_permissions", sa.String(512), nullable=True))
    add_column_if_not_exists("users", "password_hash", sa.Column("password_hash", sa.String(256), nullable=True))
    add_column_if_not_exists("users", "deposit_balance", sa.Column("deposit_balance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"))

    # tournaments
    add_column_if_not_exists("tournaments", "per_kill_prize", sa.Column("per_kill_prize", sa.Numeric(precision=12, scale=2), server_default="0.0"))
    add_column_if_not_exists("tournaments", "max_slots", sa.Column("max_slots", sa.Integer(), server_default="100"))
    add_column_if_not_exists("tournaments", "match_type", sa.Column("match_type", sa.String(), server_default="SOLO"))
    add_column_if_not_exists("tournaments", "map_name", sa.Column("map_name", sa.String(), nullable=True))
    add_column_if_not_exists("tournaments", "prize_distribution", sa.Column("prize_distribution", sa.JSON(), nullable=True))

    # wallet_transactions
    add_column_if_not_exists("wallet_transactions", "payment_mode", sa.Column("payment_mode", sa.String(), nullable=True))
    add_column_if_not_exists("wallet_transactions", "gateway_payment_id", sa.Column("gateway_payment_id", sa.String(), nullable=True))
    add_column_if_not_exists("wallet_transactions", "payu_txn_id", sa.Column("payu_txn_id", sa.String(), nullable=True))
    add_column_if_not_exists("wallet_transactions", "gateway_order_id", sa.Column("gateway_order_id", sa.String(), nullable=True))
    add_column_if_not_exists("wallet_transactions", "gateway_signature", sa.Column("gateway_signature", sa.String(), nullable=True))
    add_column_if_not_exists("wallet_transactions", "failure_reason", sa.Column("failure_reason", sa.String(), nullable=True))
    add_column_if_not_exists("wallet_transactions", "remark", sa.Column("remark", sa.String(), nullable=True))

    # tournament_participants
    add_column_if_not_exists("tournament_participants", "team_members", sa.Column("team_members", sa.JSON(), nullable=True))
    add_column_if_not_exists("tournament_participants", "slot_no", sa.Column("slot_no", sa.Integer(), nullable=True))
    add_column_if_not_exists("tournament_participants", "game_uid", sa.Column("game_uid", sa.String(), nullable=True))
    add_column_if_not_exists("tournament_participants", "account_level", sa.Column("account_level", sa.Integer(), nullable=True))
    add_column_if_not_exists("tournament_participants", "game_username", sa.Column("game_username", sa.String(), nullable=True))


def downgrade() -> None:
    pass
