"""harden participant and support constraints

Revision ID: 3fae9d91b3c0
Revises: 17bba9741c78
Create Date: 2026-03-29 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3fae9d91b3c0"
down_revision: Union[str, Sequence[str], None] = "17bba9741c78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    # Keep the oldest row for each (tournament_id, user_id) pair before adding unique constraint.
    conn.execute(sa.text(
        """
        DELETE FROM tournament_participants
        WHERE id IN (
            SELECT id FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY tournament_id, user_id
                        ORDER BY id
                    ) AS rn
                FROM tournament_participants
            ) dedupe
            WHERE dedupe.rn > 1
        )
        """
    ))

    # Normalize existing rows so the new max-length check can be applied safely.
    conn.execute(sa.text(
        """
        UPDATE chat_messages
        SET content = substr(content, 1, 1000)
        WHERE length(content) > 1000
        """
    ))

    with op.batch_alter_table("tournament_participants") as batch_op:
        batch_op.create_unique_constraint(
            "uq_tournament_participant_user",
            ["tournament_id", "user_id"],
        )

    with op.batch_alter_table("chat_messages") as batch_op:
        batch_op.create_check_constraint(
            "ck_chat_messages_content_len",
            "length(content) <= 1000",
        )
        batch_op.create_index(
            "ix_chat_messages_session_timestamp",
            ["session_id", "timestamp"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_messages") as batch_op:
        batch_op.drop_index("ix_chat_messages_session_timestamp")
        batch_op.drop_constraint("ck_chat_messages_content_len", type_="check")

    with op.batch_alter_table("tournament_participants") as batch_op:
        batch_op.drop_constraint("uq_tournament_participant_user", type_="unique")
