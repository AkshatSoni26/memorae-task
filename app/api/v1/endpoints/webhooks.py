import json
from fastapi import APIRouter, status, Request
from app.schemas.webhook import WebhookAckResponse
from app.services.session_store import session_store
from app.core.logger import logger

router = APIRouter(tags=["Webhooks"])

# Meeting BaaS also delivers account-level webhooks to the tunnel root ("POST /"); accept both.
@router.post("/", include_in_schema=False)
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

    header_event = request.headers.get("x-speaking-bot-event")
    event = body_json.get("event") or header_event or "unknown"
    data = body_json.get("data", body_json)
    bot_id = data.get("bot_id") or body_json.get("bot_id", "unknown")

    # Clean Box Formatting in Terminal
    print("\n" + "=" * 65)
    print(f"📡 [EVENT]: {event.upper()} (Bot: {bot_id})")

    # 1. Spoken by the agent
    if "utterance" in event:
        spoken_text = data.get("text", "")
        print(f"🤖 [AGENT SPOKE]: \"{spoken_text}\"")

    # 2. Transcriptions from humans speaking in the call
    if "transcript" in data or "transcription" in data or "transcript.words" in str(body_json):
        transcript_data = data.get("transcript") or data.get("transcription")
        speaker = data.get("speaker", "Participant")
        print(f"👤 [{speaker.upper()} SPOKE]: \"{transcript_data}\"")

    # 3. Status transitions (normalized to creating/joining/waiting_for_admission/in_call/ended/failed)
    status_obj = data.get("status")
    raw_status = None
    error_message = None
    if isinstance(status_obj, dict):
        raw_status = status_obj.get("code")
        error_message = status_obj.get("error_message")
    elif isinstance(status_obj, str):
        raw_status = status_obj
    elif event in ("bot.completed", "bot.failed"):
        raw_status = event
    error_message = error_message or data.get("error_message") or data.get("error")

    current_status = None
    if raw_status:
        current_status = session_store.update_status(bot_id, raw_status, error=error_message)
        print(f"📊 [STATUS]: {raw_status} -> {current_status}")
    if error_message:
        print(f"❌ [ERROR]: {error_message}")

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
