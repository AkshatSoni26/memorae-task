from typing import Optional, List, Dict
from pydantic import BaseModel, HttpUrl, Field
from app.core.config import settings
from app.models.bot import BotStatusEnum, Participant

class JoinMeetingRequest(BaseModel):
    meeting_url: HttpUrl = Field(
        ...,
        description="The Google Meet link",
        examples=["https://meet.google.com/abc-defg-hij"]
    )
    bot_name: Optional[str] = Field(
        default=None,
        description=f"Bot display name in meeting (defaults to '{settings.default_bot_name}')",
        examples=[settings.default_bot_name]
    )
    prompt: Optional[str] = Field(
        default=None,
        description="System prompt (optional: defaults to system prompt in settings)"
    )
    entry_message: Optional[str] = Field(
        default=None,
        description="Greeting message (optional: defaults to entry message in settings)"
    )
    recording_mode: Optional[str] = Field(
        default=None,
        description="speaker_view or audio_only (optional)"
    )
    webhook_url: Optional[HttpUrl] = Field(
        default=None,
        description="Webhook URL for meeting callbacks (optional)"
    )
    wake_phrases: Optional[List[str]] = Field(
        default=None,
        description="Speaking Bots only: other ways the bot's name may be heard (spellings, nicknames)"
    )
    pipeline_provider: Optional[str] = Field(
        default="groq",
        description="Voice pipeline engine: 'groq' (Groq Whisper + Qwen + Orpheus) or 'speaking_bots'",
        examples=["groq", "speaking_bots"]
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "meeting_url": "https://meet.google.com/caa-aadv-tyx",
                "bot_name": settings.default_bot_name
            }
        }
    }

class BotResponseData(BaseModel):
    bot_id: str
    status: BotStatusEnum = BotStatusEnum.JOINING
    provider_endpoint: str
    message: str

class BotLeaveResponse(BaseModel):
    bot_id: str
    message: str
    success: bool = True

class BotStatusData(BaseModel):
    bot_id: str
    status: Optional[str] = None
    meeting_url: Optional[str] = None
    video: Optional[str] = None
    audio: Optional[str] = None
    transcription: Optional[str] = None
    diarization: Optional[str] = None
    participants: List[Participant] = []
    duration_seconds: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class BotStatusResponse(BaseModel):
    bot_id: str
    status: Optional[str] = None
    data: Optional[BotStatusData] = None
    extra_details: Dict[str, str] = {}
