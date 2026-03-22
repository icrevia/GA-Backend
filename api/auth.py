from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from core.database import get_db
from core.security import hash_password, verify_password, create_access_token
from models.user import User
from schemas.user import UserCreate, UserResponse, LoginRequest
from schemas.token import Token
from typing import Any

router = APIRouter()

class SignupResponse(Token):
    user: UserResponse

@router.post("/signup", response_model=SignupResponse)
def signup(user_in: UserCreate, db: Session = Depends(get_db)) -> Any:
    # 1. Email check
    if db.query(User).filter(User.email == user_in.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # 2. Username check
    if db.query(User).filter(User.username == user_in.username).first():
        raise HTTPException(status_code=400, detail="Username taken")
    
    # 3. Create user
    db_user = User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        role="USER",
        # Default game ids can be null/empty
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    # 4. Return token + user
    return {
        "access_token": create_access_token({"sub": str(db_user.id)}),
        "token_type": "bearer",
        "role": db_user.role,
        "user": db_user
    }

@router.post("/login", response_model=SignupResponse)
def login(login_data: LoginRequest, db: Session = Depends(get_db)) -> Any:
    # 1. Find user by email
    user = db.query(User).filter(User.email == login_data.email).first()
    
    # 2. Verify password
    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 3. Increase token/login success
    return {
        "access_token": create_access_token({"sub": str(user.id)}),
        "token_type": "bearer",
        "role": user.role,
        "user": user
    }
