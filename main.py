from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from core.config import settings
from api.router import api_router
from core.database import engine, Base, SessionLocal
from models import user, tournament, wallet, support 

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # This specifically helps us debug
    try:
        print(f"DEBUG: Request Path: {request.url.path}")
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

# Re-ensure admin exists safely
from core.security import hash_password
db = SessionLocal()
try:
    admin = db.query(user.User).filter(user.User.email == "admin@zxtni.app").first()
    if not admin:
        admin = user.User(
            email="admin@zxtni.app",
            username="admin",
            hashed_password=hash_password("admin123"), # Change ASAP
            role="ADMIN",
            is_active=True
        )
        db.add(admin)
        db.commit()
        print("DEBUG: Fixed missing admin account")
except:
    pass
finally:
    db.close()

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": "ZexPlay API — Production Ready"}
