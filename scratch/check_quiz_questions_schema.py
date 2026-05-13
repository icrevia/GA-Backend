import asyncio
from sqlalchemy import text
from core.database import engine

async def check_schema():
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'quiz_questions'"))
        columns = [row[0] for row in result.fetchall()]
        print(f"Columns in quiz_questions: {columns}")

if __name__ == "__main__":
    asyncio.run(check_schema())
