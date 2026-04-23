"""
One-off migration: adds prize_distribution JSON column to tournaments table.
Run with: py migrate_prize_distribution.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from core.config import settings
import psycopg2

db_url = str(settings.DATABASE_URL)
# Convert sqlalchemy URL to psycopg2 connection string
# e.g. postgresql+psycopg2://user:pass@host/dbname or postgresql://...
db_url = db_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

conn = psycopg2.connect(db_url)
conn.autocommit = True
cur = conn.cursor()
cur.execute("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS prize_distribution JSONB")
cur.close()
conn.close()
print("Done: prize_distribution column added (or already exists).")
