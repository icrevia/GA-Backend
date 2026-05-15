import asyncio
from core.database import SessionLocal
from models.quiz import QuizQuestion
from sqlalchemy import select, func

async def check():
    async with SessionLocal() as db:
        res = await db.execute(select(QuizQuestion.category, func.count(QuizQuestion.id)).group_by(QuizQuestion.category))
        counts = res.all()
        print("Question counts by category:")
        for cat, count in counts:
            print(f"  {cat}: {count}")

if __name__ == "__main__":
    asyncio.run(check())
