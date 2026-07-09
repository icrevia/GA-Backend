import asyncio
from core.database import SessionLocal
from sqlalchemy import select
from models.ludo import LudoChallenge, LudoMatch

async def cleanup_orphaned():
    async with SessionLocal() as db:
        res = await db.execute(select(LudoChallenge).where(LudoChallenge.status == "PLAYING"))
        challenges = res.scalars().all()
        for c in challenges:
            c.status = "COMPLETED"
            print(f"Fixed challenge {c.id}")
            
        m_res = await db.execute(select(LudoMatch).where(LudoMatch.status == "PLAYING"))
        matches = m_res.scalars().all()
        for m in matches:
            m.status = "COMPLETED"
            print(f"Fixed match {m.id}")
            
        await db.commit()

asyncio.run(cleanup_orphaned())
