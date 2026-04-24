from sqlalchemy import create_engine, text
from core.config import settings

def get_current_version():
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    engine = create_engine(db_url)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version_num FROM alembic_version;"))
        row = result.fetchone()
        print(f"Current DB Alembic Version: {row[0] if row else 'None'}")

if __name__ == "__main__":
    get_current_version()
