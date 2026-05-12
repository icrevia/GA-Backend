import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("No DATABASE_URL found")
    exit(1)

# Ensure it's sync url
if "postgresql+asyncpg://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
elif "postgres://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    try:
        # Check if column exists first
        result = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='quiz_matches' AND column_name='match_type'"))
        if not result.fetchone():
            conn.execute(text("ALTER TABLE quiz_matches ADD COLUMN match_type VARCHAR DEFAULT 'BATTLE'"))
            conn.commit()
            print("Column match_type added successfully")
        else:
            print("Column match_type already exists")
    except Exception as e:
        print(f"Error: {e}")
