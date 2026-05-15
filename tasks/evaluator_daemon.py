import asyncio
import logging
from core.database import SessionLocal
from services.evaluator import evaluate_survivor_matches

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GamerzAdda.evaluator_daemon")

async def start_evaluator_daemon():
    logger.info("Starting Survivor Evaluator Daemon...")
    while True:
        try:
            db = SessionLocal()
            count = evaluate_survivor_matches(db)
            if count > 0:
                logger.info(f"Evaluated {count} matches.")
            db.close()
        except Exception as e:
            logger.error(f"Error in evaluator daemon: {str(e)}")
        
        # Check every 60 seconds
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(start_evaluator_daemon())
