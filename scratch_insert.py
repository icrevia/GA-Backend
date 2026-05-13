import asyncio
import os
import sys

# Ensure backend directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.database import engine
from sqlalchemy import text

async def main():
    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                INSERT INTO quiz_questions (category, question_text, options, correct_option_index, time_limit)
                VALUES ('BATTLE_1V1', 'Test Question', '["A", "B", "C", "D"]', 0, 10)
            """))
            print("Insert successful")
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(main())
