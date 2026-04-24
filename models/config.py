from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from core.database import Base

class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String, unique=True, index=True)
    config_value = Column(String)
    description = Column(String, nullable=True)

class HomePopup(Base):
    __tablename__ = "home_popups"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(120), nullable=False)
    message = Column(String(512), nullable=True)
    image_url = Column(String(500), nullable=True)
    
    button_text = Column(String(50), nullable=True) # CTA Label
    redirect_url = Column(String(500), nullable=True) # CTA Link
    
    is_active = Column(Boolean, default=True, nullable=False)
    
    # Frequency: ALWAYS, ONCE_PER_DAY, ONCE_PER_SESSION, ONCE_FOREVER
    show_frequency = Column(String(32), default="ONCE_PER_DAY", nullable=False)
    
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
