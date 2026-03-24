from pydantic import BaseModel
from typing import Optional

class SystemConfigResponse(BaseModel):
    id: int
    config_key: str
    config_value: str
    
    class Config:
        from_attributes = True

class SystemConfigUpdate(BaseModel):
    key: str
    value: str

class NotificationSendRequest(BaseModel):
    title: str
    body: str
    topic: str = "all"
