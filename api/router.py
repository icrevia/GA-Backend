from fastapi import APIRouter
from api import auth, users, tournaments, wallet, ws, admin, support, notifications, referral

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(tournaments.router, prefix="/tournaments", tags=["tournaments"])
api_router.include_router(wallet.router, prefix="/wallet", tags=["wallet"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(ws.router, prefix="/ws", tags=["websockets"])
api_router.include_router(support.router, prefix="/support", tags=["support"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(referral.router, prefix="/user/referral", tags=["referral"])
