import time
from core.database import SessionLocal
from models.tournament import Tournament
from models.participant import TournamentParticipant
from sqlalchemy import func, or_
import logging

logging.basicConfig()
# logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

def test_counts():
    db = SessionLocal()
    
    st = time.time()
    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.user_id == 2
    ).all()
    print(f"Fetch participants: {(time.time() - st)*1000:.2f}ms. Found {len(participants)}")
    tournament_ids = [p.tournament_id for p in participants]
    
    if not tournament_ids:
        print("No tournaments joined")
        return
        
    st = time.time()
    tournaments = db.query(Tournament).filter(Tournament.id.in_(tournament_ids)).all()
    q_time = (time.time() - st) * 1000
    print(f"Tournaments fetch time: {q_time:.2f}ms. Total: {len(tournaments)}")

    if not tournaments:
        return
        
    t_ids = [t.id for t in tournaments]
    st2 = time.time()
    rows = (
        db.query(
            TournamentParticipant.tournament_id,
            func.count(func.distinct(TournamentParticipant.slot_no)),
        )
        .filter(TournamentParticipant.tournament_id.in_(t_ids))
        .group_by(TournamentParticipant.tournament_id)
        .all()
    )
    c_time = (time.time() - st2) * 1000
    print(f"Counts fetch time: {c_time:.2f}ms")

test_counts()
