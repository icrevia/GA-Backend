import os
from sqlalchemy import create_engine, text
from core.config import settings

def rename_rank_column():
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    engine = create_engine(db_url)
    with engine.connect() as conn:
        print("Renaming column 'rank' to 'participant_rank' in 'tournament_participants' table...")
        try:
            conn.execute(text("ALTER TABLE tournament_participants RENAME COLUMN rank TO participant_rank;"))
            conn.commit()
            print("Successfully renamed column.")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    rename_rank_column()
