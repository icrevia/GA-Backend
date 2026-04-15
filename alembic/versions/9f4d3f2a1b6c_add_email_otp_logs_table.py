"""add email otp logs table

Revision ID: 9f4d3f2a1b6c
Revises: eeccdc942bc2
Create Date: 2026-04-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9f4d3f2a1b6c"
down_revision: Union[str, None] = "eeccdc942bc2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_otp_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone_number", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=220), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_email_otp_logs_id"), "email_otp_logs", ["id"], unique=False)
    op.create_index(op.f("ix_email_otp_logs_user_id"), "email_otp_logs", ["user_id"], unique=False)
    op.create_index(op.f("ix_email_otp_logs_email"), "email_otp_logs", ["email"], unique=False)
    op.create_index(op.f("ix_email_otp_logs_phone_number"), "email_otp_logs", ["phone_number"], unique=False)
    op.create_index(op.f("ix_email_otp_logs_event_type"), "email_otp_logs", ["event_type"], unique=False)
    op.create_index(op.f("ix_email_otp_logs_status"), "email_otp_logs", ["status"], unique=False)
    op.create_index(op.f("ix_email_otp_logs_created_at"), "email_otp_logs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_otp_logs_created_at"), table_name="email_otp_logs")
    op.drop_index(op.f("ix_email_otp_logs_status"), table_name="email_otp_logs")
    op.drop_index(op.f("ix_email_otp_logs_event_type"), table_name="email_otp_logs")
    op.drop_index(op.f("ix_email_otp_logs_phone_number"), table_name="email_otp_logs")
    op.drop_index(op.f("ix_email_otp_logs_email"), table_name="email_otp_logs")
    op.drop_index(op.f("ix_email_otp_logs_user_id"), table_name="email_otp_logs")
    op.drop_index(op.f("ix_email_otp_logs_id"), table_name="email_otp_logs")
    op.drop_table("email_otp_logs")
