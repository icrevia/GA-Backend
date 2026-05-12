from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class QuizMatch(Base):
    __tablename__ = "quiz_matches"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    banner_url = Column(String, nullable=True)
    
    entry_fee = Column(Numeric(precision=12, scale=2), nullable=False)
    prize_pool = Column(Numeric(precision=12, scale=2), nullable=False)
    
    start_time = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String, default="UPCOMING", index=True) # UPCOMING, LIVE, COMPLETED
    match_type = Column(String, default="BATTLE", index=True) # BATTLE, TOURNAMENT, SURVIVOR
    max_participants = Column(Integer, default=100)
    questions_per_quiz = Column(Integer, default=10)
    question_pool_size = Column(Integer, default=30)
    time_per_question = Column(Integer, default=5)
    duration_seconds = Column(Integer, nullable=True)
    
    # JSON list of prize distribution e.g. [{"rank": 1, "prize": 50}]
    prize_distribution = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    questions = relationship("QuizQuestion", back_populates="quiz", cascade="all, delete-orphan")
    participants = relationship("QuizParticipant", back_populates="quiz")

class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quiz_matches.id"), nullable=True) # Nullable for Global/1v1 questions
    category = Column(String, default="ARENA", index=True) # ARENA or BATTLE_1V1
    
    question_text = Column(String, nullable=False)
    question_image_url = Column(String, nullable=True)
    # JSON list of 4 options
    options = Column(JSON, nullable=False)
    option_images = Column(JSON, nullable=True)
    correct_option_index = Column(Integer, nullable=False)
    
    # Seconds allowed for this question
    time_limit = Column(Integer, default=15)
    
    order = Column(Integer, default=0) # Sequence of questions

    quiz = relationship("QuizMatch", back_populates="questions")

class QuizParticipant(Base):
    __tablename__ = "quiz_participants"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quiz_matches.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    score = Column(Integer, default=0)
    total_time_taken = Column(Numeric(precision=12, scale=3), default=0.000) # milliseconds precision
    
    status = Column(String, default="JOINED") # JOINED, COMPLETED
    
    # Post-match stats
    xp_earned = Column(Integer, default=0)
    mmr_delta = Column(Integer, default=0) # Change in ELO rating (+/-)
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    quiz = relationship("QuizMatch", back_populates="participants")
    user = relationship("User")

class QuizResponse(Base):
    __tablename__ = "quiz_responses"
    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quiz_matches.id"), nullable=False)
    question_id = Column(Integer, ForeignKey("quiz_questions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    option_index = Column(Integer, nullable=False)
    is_correct = Column(Boolean, default=False)
    response_time_ms = Column(Integer, nullable=False) # Time taken in ms
    created_at = Column(DateTime(timezone=True), server_default=func.now())
