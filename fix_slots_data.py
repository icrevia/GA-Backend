import psycopg2

def inspect_and_fix():
    try:
        conn = psycopg2.connect("postgresql://postgres.kijfltbddmesxjjbhdbd:R%40hul007%40%23%40%23%21%21@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require")
        cur = conn.cursor()
        
        # 1. Inspect current state for team matches
        print("Inspecting participants with shared team codes but different slots...")
        cur.execute("""
            SELECT tournament_id, team_join_code, array_agg(slot_no) as slots, array_agg(user_id) as users
            FROM tournament_participants
            WHERE team_join_code IS NOT NULL
            GROUP BY tournament_id, team_join_code
            HAVING COUNT(DISTINCT slot_no) > 1;
        """)
        rows = cur.fetchall()
        for row in rows:
            print(f"Tournament {row[0]}, Team {row[1]}: Slots {row[2]}, Users {row[3]}")
            
        # 2. FIX: Consolidate slots. Everyone in a team should use the Captain's slot (the lowest ID/earliest joiner usually)
        print("\nConsolidating slots...")
        cur.execute("""
            WITH TeamCaptains AS (
                SELECT DISTINCT ON (tournament_id, team_join_code)
                    tournament_id, team_join_code, slot_no as captain_slot
                FROM tournament_participants
                WHERE team_join_code IS NOT NULL
                ORDER BY tournament_id, team_join_code, joined_at ASC
            )
            UPDATE tournament_participants p
            SET slot_no = tc.captain_slot
            FROM TeamCaptains tc
            WHERE p.tournament_id = tc.tournament_id 
              AND p.team_join_code = tc.team_join_code
              AND p.slot_no != tc.captain_slot;
        """)
        print(f"Updated {cur.rowcount} rows.")
        
        conn.commit()
        cur.close()
        conn.close()
        print("Done.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    inspect_and_fix()
