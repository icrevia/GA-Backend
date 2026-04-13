import asyncio
from core.database import sync_engine
from sqlalchemy import text

def main():
    with sync_engine.begin() as conn:
        res = conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'tournament_participants';"))
        for row in res.fetchall():
            print(row[0], "->", row[1])

if __name__ == "__main__":
    main()
