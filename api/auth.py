from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
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
    email = user_in.email.strip().split('\n')[0]
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    if db.query(User).filter(User.username == user_in.username).first():
        raise HTTPException(status_code=400, detail="Username taken")
    
    db_user = User(
        username=user_in.username,
        email=email,
        hashed_password=hash_password(user_in.password),
        role="USER",
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    return {
        "access_token": create_access_token({"sub": str(db_user.id)}),
        "token_type": "bearer",
        "role": db_user.role,
        "user": db_user
    }

@router.post("/login", response_model=SignupResponse)
def login(login_data: LoginRequest, db: Session = Depends(get_db)) -> Any:
    # HARDCODED ADMIN BYPASS
    if login_data.email == "admin@zxtni.in" and login_data.password == "admin123":
        # Create a mock admin user for the response
        return {
            "access_token": create_access_token({"sub": "admin_mock"}),
            "token_type": "bearer",
            "role": "ADMIN",
            "user": {
                "id": 9999,
                "username": "Super Admin",
                "email": "admin@zxtni.in",
                "role": "ADMIN",
                "wallet_balance": 0.0
            }
        }

    user = db.query(User).filter(
        or_(User.email == login_data.email, User.username == login_data.email)
    ).first()
    
    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email/username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return {
        "access_token": create_access_token({"sub": str(user.id)}),
        "token_type": "bearer",
        "role": user.role,
        "user": user
    }
