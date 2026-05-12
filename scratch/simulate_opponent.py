import asyncio
import websockets
import json
import sys

# Replace with your local backend URL or production URL
WS_URL = "ws://localhost:8000/api/v1/ws?token=YOUR_TEST_TOKEN"

async def simulate_opponent():
    print("🚀 Opponent Simulator Started...")
    try:
        async with websockets.connect(WS_URL) as ws:
            # 1. Join the battle pool
            join_msg = {
                "type": "join_battle",
                "entry_fee": 20
            }
            await ws.send(json.dumps(join_msg))
            print("✅ Joined Battle Pool (Entry: ₹20). Waiting for opponent...")

            # 2. Listen for events
            while True:
                msg = await ws.recv()
                data = json.loads(msg)
                
                if data.get("type") == "battle_found":
                    print(f"⚔️ MATCH FOUND! Battle ID: {data['battle_id']}")
                    print(f"👤 Opponent: {data['opponent']['username']}")
                    
                    # Simulate some social taunts
                    await asyncio.sleep(2)
                    await ws.send(json.dumps({
                        "type": "battle_taunt",
                        "opponent_id": data['opponent']['user_id'],
                        "taunt_id": "🔥"
                    }))
                    print("😜 Sent Taunt: 🔥")
                    
                elif data.get("type") == "battle_taunt":
                    print(f"📩 Received Taunt: {data['taunt_id']} from {data['from_username']}")

    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        WS_URL = sys.argv[1]
    asyncio.run(simulate_opponent())
