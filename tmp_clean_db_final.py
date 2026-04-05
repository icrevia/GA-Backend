import psycopg2

URL = "postgresql://postgres.kijfltbddmesxjjbhdbd:R%40hul007%40%23%40%23%21%21@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require"

try:
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    
    cur.execute("""
        SELECT tablename 
        FROM pg_tables 
        WHERE schemaname = 'public';
    """)
    tables = [t[0] for t in cur.fetchall()]
    
    if tables:
        print(f"Dropping tables: {tables}")
        for t in tables:
            cur.execute(f'DROP TABLE "{t}" CASCADE;')
        print("All tables dropped successfully.")
    else:
        print("No user tables found.")
    
    cur.close()
    conn.close()
except Exception as e:
    print(f"Error: {e}")
