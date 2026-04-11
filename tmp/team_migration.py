"""
Standalone migration script to add team columns to tournament_participants.
Run this on the production server where asyncpg is available:
    python tmp/team_migration.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from sqlalchemy import text
from core.database import engine  # async engine


async def run_migration():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS team_name TEXT"))
        await conn.execute(text("ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS team_join_code TEXT"))
        await conn.execute(text("ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS is_team_captain BOOLEAN DEFAULT FALSE"))

        # Add an index for fast team lookups by code
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_tp_team_join_code
            ON tournament_participants (team_join_code)
            WHERE team_join_code IS NOT NULL
        """))

        print("[✅ MIGRATION] team_name, team_join_code, is_team_captain added to tournament_participants")

if __name__ == "__main__":
    asyncio.run(run_migration())
