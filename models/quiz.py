from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class QuizMatch(Base):
    __tablename__ = "quiz_matches"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    
    entry_fee = Column(Numeric(precision=12, scale=2), nullable=False)
    prize_pool = Column(Numeric(precision=12, scale=2), nullable=False)
    
    start_time = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String, default="UPCOMING", index=True) # UPCOMING, LIVE, COMPLETED
    
    # JSON list of prize distribution e.g. [{"rank": 1, "prize": 50}]
    prize_distribution = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    questions = relationship("QuizQuestion", back_populates="quiz", cascade="all, delete-orphan")
    participants = relationship("QuizParticipant", back_populates="quiz")

class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id = Column(Integer, primary_key=True, index=True)
    quiz_id = Column(Integer, ForeignKey("quiz_matches.id"), nullable=False)
    
    question_text = Column(String, nullable=False)
    # JSON list of 4 options
    options = Column(JSON, nullable=False)
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
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    quiz = relationship("QuizMatch", back_populates="participants")
    user = relationship("User")
