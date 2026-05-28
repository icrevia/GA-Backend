import asyncio
import os
import sys

# Ensure the script can import from the backend directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.database import SessionLocal
from models.user import User
from sqlalchemy import select, delete

async def main():
    async with SessionLocal() as db:
        print("Fetching non-admin users...")
        result = await db.execute(select(User.id).where(User.role != 'admin'))
        user_ids = [row[0] for row in result.fetchall()]
        
        if not user_ids:
            print("No non-admin users found to delete.")
            return

        print(f"Found {len(user_ids)} non-admin users.")
        
        confirm = input(f"Are you sure you want to delete {len(user_ids)} users? (type 'yes' to confirm): ")
        if confirm.strip().lower() != 'yes':
            print("Operation cancelled.")
            return

        print("Deleting users... this will cascade and delete their wallet transactions, chat messages, etc. if DB constraints are set.")
        
        try:
            # Delete query
            await db.execute(delete(User).where(User.role != 'admin'))
            await db.commit()
            print("Successfully deleted all non-admin users.")
        except Exception as e:
            await db.rollback()
            print(f"Failed to delete users. Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
