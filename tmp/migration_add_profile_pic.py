from sqlalchemy import create_engine, text
from core.config import settings

def run_migration():
    engine = create_engine(settings.DATABASE_URL)
    with engine.connect() as connection:
        try:
            print("Checking if profile_pic column exists...")
            # We use text() for raw SQL
            connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_pic VARCHAR;"))
            connection.commit()
            print("Successfully added profile_pic column to users table!")
        except Exception as e:
            print(f"Error during migration: {e}")

if __name__ == "__main__":
    run_migration()
