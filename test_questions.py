import asyncio
import os
from sqlalchemy import select
from core.database import SessionLocal
from models.quiz import QuizQuestion

async def main():
    async with SessionLocal() as db:
        res = await db.execute(select(QuizQuestion))
        print(f"TOTAL QUESTIONS: {len(res.scalars().all())}")

if __name__ == "__main__":
    asyncio.run(main())
