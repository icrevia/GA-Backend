from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from core.database import Base


class HomeBanner(Base):
    __tablename__ = "home_banners"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(120), nullable=False)
    image_url = Column(String(500), nullable=False)
    redirect_url = Column(String(500), nullable=True)
    notes = Column(String(300), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)

    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
