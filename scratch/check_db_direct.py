import psycopg2
import os
from urllib.parse import urlsplit

def check_db():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not found")
        return

    # Convert to psycopg2 format if needed
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    
    # Remove sslmode=require for local psycopg2 if not supported, 
    # but usually it is.
    
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        print("--- QUIZ MATCHES ---")
        cur.execute("SELECT id, title, status, start_time FROM quiz_matches")
        for row in cur.fetchall():
            print(row)
            
        print("\n--- QUIZ QUESTIONS (Quiz ID 1) ---")
        cur.execute("SELECT id, question_text, question_image_url, options, option_images FROM quiz_questions WHERE quiz_id = 1")
        for row in cur.fetchall():
            print(row)
            
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_db()
