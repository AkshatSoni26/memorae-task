from fastapi import APIRouter, HTTPException, status
from app.schemas.bot import (
    JoinMeetingRequest,
    BotResponseData,
    BotStatusResponse,
    BotLeaveResponse
)
from app.services.meeting_baas import meeting_baas_service
from app.core.exceptions import MeetingServiceError

router = APIRouter(prefix="/bots", tags=["Bots"])

@router.post(
    "/join",
    response_model=BotResponseData,
    status_code=status.HTTP_201_CREATED,
    summary="Join Google Meet"
)
def join_meeting(request: JoinMeetingRequest):
    """
    Accepts a Google Meet link and instructions, then dispatches the bot to join the meeting.
    """
    try:
        return meeting_baas_service.dispatch_bot(request)
    except MeetingServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

@router.get(
    "/{bot_id}",
    response_model=BotStatusResponse,
    summary="Get Bot Status"
)
def get_bot_status(bot_id: str):
    """
    Retrieves the current status of the bot inside the meeting.
    """
    try:
        return meeting_baas_service.get_status(bot_id)
    except MeetingServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

@router.delete(
    "/{bot_id}",
    response_model=BotLeaveResponse,
    summary="Leave Meeting"
)
def leave_meeting(bot_id: str):
    """
    Instructs the bot to leave the Google Meet meeting.
    """
    try:
        return meeting_baas_service.leave_meeting(bot_id)
    except MeetingServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
