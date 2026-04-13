import asyncio
from core.database import get_db_sync
from models.user import User
from api.tournaments import get_my_tournaments
from schemas.tournament import TournamentResponse
from pydantic import TypeAdapter
from typing import List
import time
import logging

logging.basicConfig(level=logging.INFO)

def test():
    db = next(get_db_sync())
    user = db.query(User).filter(User.id == 2).first()
    
    st = time.perf_counter()
    tournaments = get_my_tournaments(db, user)
    
    t1 = time.perf_counter()
    adapter = TypeAdapter(List[TournamentResponse])
    out = adapter.validate_python(tournaments)
    t2 = time.perf_counter()
    
    print(f"Total time DB: {(t1 - st)*1000:.1f}ms")
    print(f"Total time Pydantic: {(t2 - t1)*1000:.1f}ms")

if __name__ == "__main__":
    test()
