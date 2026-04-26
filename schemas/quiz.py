from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import List, Optional
from decimal import Decimal

class QuizQuestionResponse(BaseModel):
    id: int
    question_text: str
    options: List[str]
    time_limit: int
    order: int
    model_config = ConfigDict(from_attributes=True)

class QuizMatchResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    entry_fee: Decimal
    prize_pool: Decimal
    start_time: datetime
    status: str
    max_participants: int = 100
    prize_distribution: Optional[List[dict]] = None
    joined_count: int = 0
    is_joined: bool = False
    
    model_config = ConfigDict(from_attributes=True)

class QuizJoinResponse(BaseModel):
    message: str
    new_wallet_balance: float
    quiz_id: int
    deduction_breakdown: Optional[dict] = None
