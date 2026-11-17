from typing import Optional, Dict
from pydantic import BaseModel

class WebhookStatusInfo(BaseModel):
    code: str
    created_at: Optional[str] = None
    start_time: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None

class WebhookData(BaseModel):
    bot_id: str
    status: Optional[WebhookStatusInfo] = None
    meeting_url: Optional[str] = None
    transcript_url: Optional[str] = None
    recording_url: Optional[str] = None

class MeetingBaaSWebhookPayload(BaseModel):
    event: str
    data: WebhookData

class WebhookAckResponse(BaseModel):
    received: bool = True
    event: str
    bot_id: str
    current_status: Optional[str] = None
