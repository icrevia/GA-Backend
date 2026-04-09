from sqlalchemy import Boolean, Column, DateTime, Integer, Numeric, String
from sqlalchemy.sql import func

from core.database import Base


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(40), unique=True, index=True, nullable=False)

    # Numeric(12,2): exact money precision
    discount_amount = Column(Numeric(precision=12, scale=2), nullable=False)

    uses_count = Column(Integer, default=0, nullable=False)
    max_uses = Column(Integer, default=100, nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    notes = Column(String(300), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
