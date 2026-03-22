from sqlalchemy import Column, Integer, String, Boolean
from core.database import Base

class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String, unique=True, index=True)
    config_value = Column(String)
    description = Column(String, nullable=True)
