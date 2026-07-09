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
    tables = inspector.get_table_names()

    if 'quiz_matches' not in tables:
        op.create_table(
            'quiz_matches',
            sa.Column('id', sa.Integer(), primary_key=True, index=True, autoincrement=True),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('description', sa.String(), nullable=True),
            sa.Column('banner_url', sa.String(), nullable=True),
            sa.Column('entry_fee', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('prize_pool', sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
            sa.Column('status', sa.String(), server_default='UPCOMING'),
            sa.Column('match_type', sa.String(), server_default='BATTLE'),
            sa.Column('max_participants', sa.Integer(), server_default='100'),
            sa.Column('questions_per_quiz', sa.Integer(), server_default='10'),
            sa.Column('question_pool_size', sa.Integer(), server_default='30'),
            sa.Column('time_per_question', sa.Integer(), server_default='5'),
            sa.Column('duration_seconds', sa.Integer(), nullable=True),
            sa.Column('end_time', sa.DateTime(timezone=True), nullable=True),
            sa.Column('evaluation_status', sa.String(), server_default='PENDING'),
            sa.Column('prize_distribution', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True)
        )
        op.create_index(op.f('ix_quiz_matches_id'), 'quiz_matches', ['id'], unique=False)
        op.create_index(op.f('ix_quiz_matches_start_time'), 'quiz_matches', ['start_time'], unique=False)
        op.create_index(op.f('ix_quiz_matches_status'), 'quiz_matches', ['status'], unique=False)
        op.create_index(op.f('ix_quiz_matches_match_type'), 'quiz_matches', ['match_type'], unique=False)
        op.create_index(op.f('ix_quiz_matches_end_time'), 'quiz_matches', ['end_time'], unique=False)
    else:
        columns = [c['name'] for c in inspector.get_columns('quiz_matches')]
        if 'banner_url' not in columns:
            op.add_column('quiz_matches', sa.Column('banner_url', sa.String(), nullable=True))

    if 'quiz_questions' not in tables:
        op.create_table(
            'quiz_questions',
            sa.Column('id', sa.Integer(), primary_key=True, index=True, autoincrement=True),
            sa.Column('quiz_id', sa.Integer(), sa.ForeignKey('quiz_matches.id'), nullable=True),
            sa.Column('category', sa.String(), server_default='ARENA'),
            sa.Column('question_text', sa.String(), nullable=False),
            sa.Column('question_image_url', sa.String(), nullable=True),
            sa.Column('options', sa.JSON(), nullable=False),
            sa.Column('option_images', sa.JSON(), nullable=True),
            sa.Column('correct_option_index', sa.Integer(), nullable=False),
            sa.Column('time_limit', sa.Integer(), server_default='15'),
            sa.Column('order', sa.Integer(), server_default='0')
        )
        op.create_index(op.f('ix_quiz_questions_id'), 'quiz_questions', ['id'], unique=False)
        op.create_index(op.f('ix_quiz_questions_category'), 'quiz_questions', ['category'], unique=False)

    if 'quiz_participants' not in tables:
        op.create_table(
            'quiz_participants',
            sa.Column('id', sa.Integer(), primary_key=True, index=True, autoincrement=True),
            sa.Column('quiz_id', sa.Integer(), sa.ForeignKey('quiz_matches.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('score', sa.Integer(), server_default='0'),
            sa.Column('total_time_taken', sa.Numeric(precision=12, scale=3), server_default='0.000'),
            sa.Column('rank', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(), server_default='JOINED'),
            sa.Column('xp_earned', sa.Integer(), server_default='0'),
            sa.Column('mmr_delta', sa.Integer(), server_default='0'),
            sa.Column('joined_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
            sa.Column('user_start_time', sa.DateTime(timezone=True), nullable=True)
        )
        op.create_index(op.f('ix_quiz_participants_id'), 'quiz_participants', ['id'], unique=False)

    if 'quiz_responses' not in tables:
        op.create_table(
            'quiz_responses',
            sa.Column('id', sa.Integer(), primary_key=True, index=True, autoincrement=True),
            sa.Column('quiz_id', sa.Integer(), sa.ForeignKey('quiz_matches.id'), nullable=False),
            sa.Column('question_id', sa.Integer(), sa.ForeignKey('quiz_questions.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('option_index', sa.Integer(), nullable=False),
            sa.Column('is_correct', sa.Boolean(), server_default='false'),
            sa.Column('response_time_ms', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'))
        )
        op.create_index(op.f('ix_quiz_responses_id'), 'quiz_responses', ['id'], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('quiz_matches')]
    if 'banner_url' in columns:
        op.drop_column('quiz_matches', 'banner_url')
