from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from core.config import settings
from api.router import api_router

from core.database import engine, Base, SessionLocal
from models import user, tournament, wallet 
from core.security import hash_password

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # This specifically helps us debug why signup returns 422 on production
    try:
        body = await request.body()
        if body:
            print(f"DEBUG: Request Path: {request.url.path}")
            print(f"DEBUG: Request Body: {body.decode()}")
    except:
        pass
    response = await call_next(request)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize DB
Base.metadata.create_all(bind=engine)

# Admin Recovery Block
db = SessionLocal()
try:
    existing_admin = db.query(user.User).filter(user.User.role == "ADMIN").first()
    if not existing_admin:
        new_admin = user.User(
            username="Admin",
            email="admin@zxtni.in",
            hashed_password=hash_password("admin123"), # Default password
            role="ADMIN",
            is_active=True
        )
        db.add(new_admin)
        db.commit()
finally:
    db.close()

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": "ZexPlay API — Production Ready"}
