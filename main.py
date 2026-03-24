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

# Initialize DB without destructive blocks
Base.metadata.create_all(bind=engine)

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": "ZexPlay API — Production Ready"}
