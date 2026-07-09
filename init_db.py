import asyncio
import logging
import sys
from sqlalchemy import text
from core.database import engine, Base
import os
import subprocess

# Import all models to ensure they are registered with Base.metadata
import models.user
import models.banner
import models.tournament
import models.wallet
import models.withdraw_upi_account
import models.participant
import models.config
import models.promo
import models.restriction
import models.otp_phone_lock
import models.user_activity_lock
import models.support
import models.notification
import models.admin_access_session
import models.quiz
import models.daily_stats
import models.pending_otp
import models.ludo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("init_db")

async def init_db():
    logger.info("Checking database state...")
    try:
        is_fresh_db = False
        async with engine.begin() as conn:
            # Check if alembic_version table exists
            result = await conn.execute(text(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'alembic_version')"
            ))
            alembic_exists = result.scalar()
            
            # Check if chat_messages exists
            result = await conn.execute(text(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'chat_messages')"
            ))
            chat_messages_exists = result.scalar()
            
            if not alembic_exists or not chat_messages_exists:
                logger.info("Fresh or broken database detected. Creating all tables from models...")
                await conn.run_sync(Base.metadata.create_all)
                is_fresh_db = True
            else:
                logger.info("Database is fully initialized. Skipping table creation.")
                
        if is_fresh_db:
            logger.info("Stamping alembic head...")
            subprocess.run(["alembic", "stamp", "head"], check=True)
            logger.info("Successfully created all tables and stamped alembic.")

    except Exception as e:
        logger.error(f"Error during database initialization: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(init_db())
