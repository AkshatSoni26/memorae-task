import json
from fastapi import APIRouter, status, Request
from app.schemas.webhook import WebhookAckResponse
from app.services.session_store import session_store
from app.core.logger import logger
from app.core.config import settings

router = APIRouter(tags=["Webhooks"])

@router.post(
    "/webhook",
    response_model=WebhookAckResponse,
    status_code=status.HTTP_200_OK,
    summary="Handle Meeting BaaS webhook callbacks"
)
async def handle_meeting_baas_webhook(request: Request):
    """
    Receives real-time webhook events and live utterances from Meeting BaaS.
    """
    raw_body = await request.body()
    try:
        body_json = json.loads(raw_body.decode("utf-8"))
    except Exception:
        body_json = {}

    event = body_json.get("event", "unknown")
    data = body_json.get("data", {})
    bot_id = body_json.get("bot_id") or data.get("bot_id", "unknown")

    logger.info("=" * 65)
    logger.info(f"🔔 [WEBHOOK EVENT]: '{event}' | Bot ID: {bot_id}")

    # 1. Spoken utterances / speech events
    if event == "speaking_bot.utterance" or "utterance" in event:
        spoken_text = data.get("text", "")
        logger.info(f"🗣️ [AGENT SPOKE]: \"{spoken_text}\"")

    # 2. Status change events
    status_obj = data.get("status")
    current_status = None
    if isinstance(status_obj, dict):
        current_status = status_obj.get("code")
        error_msg = status_obj.get("error_message")
        logger.info(f"📊 [STATUS CODE]: {current_status}")
        if error_msg:
            logger.error(f"❌ [STATUS ERROR]: {error_msg}")
        session_store.update_status(bot_id, current_status)
    elif isinstance(status_obj, str):
        current_status = status_obj
        logger.info(f"📊 [STATUS CODE]: {current_status}")
        session_store.update_status(bot_id, current_status)

    # 3. Chat / Transcription messages
    if "message" in data:
        sender = data.get("sender", "participant")
        msg_text = data.get("message")
        logger.info(f"💬 [CHAT MESSAGE] From: {sender} -> \"{msg_text}\"")

    transcript = data.get("transcript") or data.get("transcription")
    if transcript:
        logger.info(f"📝 [TRANSCRIPT/SPEECH]: {transcript}")

    # 4. Print raw payload for maximum visibility
    logger.info(f"📦 [RAW PAYLOAD]: {json.dumps(body_json, indent=2)}")
    logger.info("=" * 65)

    return WebhookAckResponse(
        received=True,
        event=event,
        bot_id=bot_id,
        current_status=current_status
    )
