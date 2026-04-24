import os
from sqlalchemy import create_engine, text, inspect
from core.config import settings

def check_columns():
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    engine = create_engine(db_url)
    inspector = inspect(engine)
    columns = inspector.get_columns("tournament_participants")
    print(f"Columns in 'tournament_participants': {[c['name'] for c in columns]}")

if __name__ == "__main__":
    check_columns()
