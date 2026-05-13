import asyncio
import os
import sys

# Ensure backend directory is in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.database import SessionLocal, engine
from models.quiz import QuizQuestion
from schemas.admin import QuizQuestionCreate

async def main():
    data = QuizQuestionCreate(
        question_text="Which game features a battle royale mode called 'Warzone'?",
        question_image_url="https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&q=80&w=800",
        options=["PUBG", "Free Fire", "Call of Duty", "Fortnite"],
        correct_option_index=2,
        time_limit=10
    )
    
    async with SessionLocal() as db:
        try:
            q = QuizQuestion(
                **data.dict(),
                category="BATTLE_1V1",
                quiz_id=None
            )
            db.add(q)
            await db.commit()
            await db.refresh(q)
            print("Insert successful, ID:", q.id)
        except Exception as e:
            print(f"Exception type: {type(e)}")
            print(f"Exception message: {str(e)}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
