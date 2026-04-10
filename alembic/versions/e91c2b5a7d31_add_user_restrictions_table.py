"""add user restrictions table

Revision ID: e91c2b5a7d31
Revises: c4a9bd4f2191
Create Date: 2026-04-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e91c2b5a7d31"
down_revision: Union[str, Sequence[str], None] = "c4a9bd4f2191"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_restrictions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("page_key", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("lifted_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("lift_note", sa.String(length=300), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_admin_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["lifted_by_admin_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_restrictions_id", "user_restrictions", ["id"], unique=False)
    op.create_index("ix_user_restrictions_user_id", "user_restrictions", ["user_id"], unique=False)
    op.create_index("ix_user_restrictions_scope", "user_restrictions", ["scope"], unique=False)
    op.create_index("ix_user_restrictions_page_key", "user_restrictions", ["page_key"], unique=False)
    op.create_index("ix_user_restrictions_is_active", "user_restrictions", ["is_active"], unique=False)
    op.create_index("ix_user_restrictions_created_by_admin_id", "user_restrictions", ["created_by_admin_id"], unique=False)
    op.create_index("ix_user_restrictions_lifted_by_admin_id", "user_restrictions", ["lifted_by_admin_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_restrictions_lifted_by_admin_id", table_name="user_restrictions")
    op.drop_index("ix_user_restrictions_created_by_admin_id", table_name="user_restrictions")
    op.drop_index("ix_user_restrictions_is_active", table_name="user_restrictions")
    op.drop_index("ix_user_restrictions_page_key", table_name="user_restrictions")
    op.drop_index("ix_user_restrictions_scope", table_name="user_restrictions")
    op.drop_index("ix_user_restrictions_user_id", table_name="user_restrictions")
    op.drop_index("ix_user_restrictions_id", table_name="user_restrictions")
    op.drop_table("user_restrictions")
