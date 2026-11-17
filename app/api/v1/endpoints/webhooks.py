import json
from fastapi import APIRouter, status, Request
from app.schemas.webhook import WebhookAckResponse
from app.services.session_store import session_store
from app.core.logger import logger

router = APIRouter(tags=["Webhooks"])

@router.post(
    "/webhook",
    response_model=WebhookAckResponse,
    status_code=status.HTTP_200_OK,
    summary="Handle Meeting BaaS webhook callbacks"
)
async def handle_meeting_baas_webhook(request: Request):
    """
    Receives all real-time events from Meeting BaaS and prints clean conversational logs.
    """
    raw_body = await request.body()
    try:
        body_json = json.loads(raw_body.decode("utf-8"))
    except Exception:
        body_json = {}

    event = body_json.get("event", "unknown")
    data = body_json.get("data", body_json)
    bot_id = data.get("bot_id") or body_json.get("bot_id", "unknown")

    # Clean Box Formatting in Terminal
    print("\n" + "=" * 65)
    print(f"📡 [EVENT]: {event.upper()} (Bot: {bot_id})")

    # 1. Spoken by the agent
    if event == "speaking_bot.utterance" or "utterance" in event:
        spoken_text = data.get("text", "")
        print(f"🤖 [AGENT SPOKE]: \"{spoken_text}\"")

    # 2. Transcriptions from humans speaking in the call
    if "transcript" in data or "transcription" in data or "transcript.words" in str(body_json):
        transcript_data = data.get("transcript") or data.get("transcription")
        speaker = data.get("speaker", "Participant")
        print(f"👤 [{speaker.upper()} SPOKE]: \"{transcript_data}\"")

    # 3. Status transitions
    status_obj = data.get("status")
    current_status = None
    if isinstance(status_obj, dict):
        current_status = status_obj.get("code")
        print(f"📊 [STATUS]: {current_status}")
        if status_obj.get("error_message"):
            print(f"❌ [ERROR]: {status_obj.get('error_message')}")
        session_store.update_status(bot_id, current_status)
    elif isinstance(status_obj, str):
        current_status = status_obj
        print(f"📊 [STATUS]: {current_status}")
        session_store.update_status(bot_id, current_status)

    # 4. In-meeting chat messages
    if "message" in data:
        sender = data.get("sender", "Participant")
        print(f"💬 [CHAT] {sender}: {data.get('message')}")

    print("=" * 65 + "\n")

    return WebhookAckResponse(
        received=True,
        event=event,
        bot_id=bot_id,
        current_status=current_status
    )
