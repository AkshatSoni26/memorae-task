from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, HttpUrl

class BotStatusEnum(str, Enum):
    CREATING = "creating"
    JOINING = "joining"
    WAITING_FOR_ADMISSION = "waiting_for_admission"
    IN_WAITING_ROOM = "in_waiting_room"
    IN_CALL = "in_call"
    RECORDING = "recording"
    COMPLETED = "completed"
    ENDED = "ended"
    FAILED = "failed"

class Participant(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    display_name: Optional[str] = None
    profile_picture: Optional[str] = None

class BotDetails(BaseModel):
    bot_id: str
    status: Optional[str] = None
    meeting_url: Optional[str] = None
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    transcription_url: Optional[str] = None
    diarization_url: Optional[str] = None
    participants: List[Participant] = []
    duration_seconds: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class BotSession(BaseModel):
    bot_id: str
    meeting_url: HttpUrl
    bot_name: str
    status: BotStatusEnum = BotStatusEnum.JOINING
