import asyncio
from core.database import SessionLocal
from models.ludo import LudoChallenge, LudoMatch
from sqlalchemy import select

async def run():
    db = SessionLocal()
    res = await db.execute(select(LudoChallenge).join(LudoMatch, LudoChallenge.match_id == LudoMatch.id).where(LudoChallenge.status.in_(['PLAYING', 'WAITING_SYNC']), LudoMatch.status == 'COMPLETED'))
    challenges = res.scalars().all()
    print('Found', len(challenges))
    for c in challenges:
        c.status = 'COMPLETED'
    await db.commit()
    await db.close()

asyncio.run(run())
