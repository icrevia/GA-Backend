import asyncio
from core.database import engine
from sqlalchemy import text

async def main():
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'quiz_questions';
        """))
        for row in result:
            print(f"{row[0]}: {row[1]}")

asyncio.run(main())
