from models.user import User
from models.banner import HomeBanner
from models.tournament import Tournament
from models.wallet import WalletTransaction
from models.withdraw_upi_account import WithdrawUpiAccount
from models.participant import TournamentParticipant
from models.config import SystemConfig
from models.promo import PromoCode
from models.restriction import UserRestriction
from models.otp_phone_lock import OtpPhoneLock
from models.user_activity_lock import UserActivityLock
from models.support import ChatSession, ChatMessage
from models.notification import Notification
from models.admin_access_session import AdminAccessSession
from models.quiz import QuizMatch, QuizQuestion, QuizParticipant

# This file is used to import all models so Alembic can discover them
