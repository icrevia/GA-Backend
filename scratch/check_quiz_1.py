from core.database import SyncSessionLocal
from models.quiz import QuizMatch, QuizQuestion, QuizParticipant

def check_quiz_data(quiz_id):
    db = SyncSessionLocal()
    try:
        quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
        if not quiz:
            print(f"Quiz {quiz_id} not found")
            return
        
        print(f"Quiz: {quiz.title}, Status: {quiz.status}, Start Time: {quiz.start_time}")
        
        questions = db.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz_id).all()
        print(f"Total Questions: {len(questions)}")
        for q in questions:
            print(f"  - Q: {q.question_text}, Image: {q.question_image_url}")
            print(f"    Options: {q.options}")
            print(f"    Correct: {q.correct_option_index}")
        
        participants = db.query(QuizParticipant).filter(QuizParticipant.quiz_id == quiz_id).all()
        print(f"Total Participants: {len(participants)}")
        for p in participants:
            print(f"  - User ID: {p.user_id}")
            
    finally:
        db.close()

if __name__ == "__main__":
    check_quiz_data(1)
