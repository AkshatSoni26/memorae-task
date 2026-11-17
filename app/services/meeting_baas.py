import requests
from requests.exceptions import (
    Timeout,
    ConnectionError as ReqConnectionError,
    RequestException
)
from app.core.config import settings
from app.core.logger import logger
from app.core.exceptions import (
    MeetingConfigurationError,
    MeetingProviderNetworkError,
    MeetingProviderResponseError,
    BotNotFoundError
)
from app.schemas.bot import (
    JoinMeetingRequest,
    BotResponseData,
    BotStatusResponse,
    BotStatusData,
    BotLeaveResponse
)
from app.models.bot import BotStatusEnum, Participant
from app.helpers.meeting import sanitize_meeting_url
from app.services.session_store import session_store

def get_discovered_webhook_url() -> str | None:
    """
    Dynamically discovers the current active public webhook endpoint:
    1. Checks explicit WEBHOOK_URL / PUBLIC_URL environment variable if set.
    2. Queries live ngrok local API (http://127.0.0.1:4040/api/tunnels) for the currently running tunnel.
    3. Returns None if no public endpoint is running (never uses stale hardcoded domains).
    """
    import os
    # 1. Environment variable override
    env_url = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("PUBLIC_URL")
        or getattr(settings, "webhook_url", "")
    )
    if env_url and env_url.strip():
        cleaned = env_url.strip().rstrip("/")
        return f"{cleaned}/webhook" if not cleaned.endswith("/webhook") else cleaned

    # 2. Dynamic auto-discovery from active local ngrok tunnel
    for host in ["127.0.0.1", "localhost"]:
        try:
            resp = requests.get(f"http://{host}:4040/api/tunnels", timeout=1.5)
            if resp.ok:
                tunnels = resp.json().get("tunnels", [])
                for tunnel in tunnels:
                    if tunnel.get("proto") == "https":
                        public_url = tunnel.get("public_url", "").rstrip("/")
                        if public_url:
                            logger.info(f"🌐 Dynamically discovered active ngrok tunnel: {public_url}")
                            return f"{public_url}/webhook"
        except Exception:
            pass

    logger.warning("⚠️ No active public webhook URL found (ngrok not running on port 4040 and WEBHOOK_URL not set).")
    return None

class MeetingBaaSClient:
    def __init__(self):
        self.api_key: str = settings.meeting_baas_api_key
        self.headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-meeting-baas-api-key": self.api_key
        }

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            logger.error("Meeting BaaS API key is missing")
            raise MeetingConfigurationError("MEETING_BAAS API key is not configured in .env")

    def dispatch_bot(self, request: JoinMeetingRequest) -> BotResponseData:
        self._ensure_api_key()
        cleaned_url: str = sanitize_meeting_url(str(request.meeting_url))

        def clean_val(val: str | None, fallback: str) -> str:
            if not val or val.strip().lower() == "string":
                return fallback
            return val.strip()

        bot_name: str = clean_val(request.bot_name, settings.default_bot_name)
        entry_message: str = clean_val(request.entry_message, settings.default_entry_message)
        recording_mode: str = clean_val(request.recording_mode, settings.default_recording_mode)

        # Standard Pipecat/Speaking-Bots conversational prompt
        system_prompt = (
            f"You are a helpful AI participant in this meeting. Your name is {bot_name}.\n\n"
            f"Rules:\n"
            f"- Whenever anyone addresses you by name ({bot_name}) or asks you a question, you MUST answer verbally and out loud.\n"
            f"- Keep your answers concise, conversational, and direct (1-2 sentences max).\n"
            f"- Answer the user's question directly without hesitation."
        )

        logger.info(f"🚀 Dispatching conversational agent '{bot_name}' to meeting: {cleaned_url}")

        callback_url = None
        if request.webhook_url and str(request.webhook_url).lower() != "https://example.com/":
            callback_url = str(request.webhook_url)
        else:
            callback_url = get_discovered_webhook_url()

        payload_speaking: dict = {
            "meeting_url": cleaned_url,
            "bot_name": bot_name,
            "prompt": system_prompt,
            "personas": ["helpful_assistant"],
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "entry_message": entry_message,
            "enable_tools": False,
            "respond_mode": "always",
            "turn_config": {
                "stop_secs": 0.4,
                "unfinished_pause_secs": 0.8
            },
            "briefing": {}
        }
        if callback_url:
            payload_speaking["callback_url"] = callback_url

        try:
            logger.info(f"📡 Sending request to Speaking Bots API: {settings.speaking_api_url}")
            resp = requests.post(
                settings.speaking_api_url,
                json=payload_speaking,
                headers=self.headers,
                timeout=15
            )
            if resp.ok:
                resp_json = resp.json()
                bot_id_val = str(resp_json.get("bot_id") or resp_json.get("id") or resp_json)
                session_store.register_bot(bot_id_val, cleaned_url, bot_name)
                session_store.update_status(bot_id_val, "in_call")
                logger.info(f"✅ Bot '{bot_name}' dispatched successfully via Speaking Bots API! Bot ID: {bot_id_val}")
                return BotResponseData(
                    bot_id=bot_id_val,
                    status=BotStatusEnum.JOINING,
                    provider_endpoint=settings.speaking_api_url,
                    message=f"Bot '{bot_name}' successfully dispatched via Speaking Bots API."
                )
            else:
                logger.warning(f"⚠️ Speaking Bots API returned status {resp.status_code}: {resp.text}")
        except (Timeout, ReqConnectionError) as net_err:
            logger.warning(f"⚠️ Speaking Bots network error, falling back to standard API: {net_err}")

        # Fallback to standard Meeting BaaS v2 API
        logger.info(f"📡 Falling back to standard Meeting BaaS v2 API: {settings.baas_api_url}")
        payload_v2: dict[str, str] = {
            "meeting_url": cleaned_url,
            "bot_name": bot_name,
            "recording_mode": recording_mode,
            "entry_message": entry_message,
        }
        if request.webhook_url and str(request.webhook_url).lower() != "https://example.com/":
            payload_v2["webhook_url"] = str(request.webhook_url)

        try:
            resp = requests.post(
                settings.baas_api_url,
                json=payload_v2,
                headers=self.headers,
                timeout=15
            )
            if resp.ok:
                resp_json = resp.json()
                bot_id_val = str(resp_json.get("bot_id") or resp_json.get("id") or resp_json)
                session_store.register_bot(bot_id_val, cleaned_url, bot_name)
                logger.info(f"✅ Bot '{bot_name}' dispatched via Meeting BaaS v2 API. Bot ID: {bot_id_val}")
                return BotResponseData(
                    bot_id=bot_id_val,
                    status=BotStatusEnum.JOINING,
                    provider_endpoint=settings.baas_api_url,
                    message=f"Bot '{bot_name}' successfully dispatched via Meeting BaaS v2 API."
                )
            logger.error(f"❌ Meeting BaaS v2 API error ({resp.status_code}): {resp.text}")
            raise MeetingProviderResponseError(
                status_code=resp.status_code,
                detail=resp.text
            )
        except (Timeout, ReqConnectionError) as net_err:
            logger.error(f"❌ Network failure dispatching bot: {net_err}")
            raise MeetingProviderNetworkError(f"Connection timeout or network failure: {str(net_err)}")
        except RequestException as req_err:
            logger.error(f"❌ HTTP request error: {req_err}")
            raise MeetingProviderNetworkError(f"HTTP request error: {str(req_err)}")

    def get_bot_usage(self, bot_id: str) -> dict | None:
        """
        Retrieves live pipeline diagnostic metrics (audio seconds, turns, TTS plays)
        """
        url = f"{settings.speaking_api_url}/{bot_id}/usage"
        try:
            resp = requests.get(url, headers=self.headers, timeout=5)
            if resp.ok:
                return resp.json()
        except Exception:
            pass
        return None

    def get_status(self, bot_id: str) -> BotStatusResponse:
        self._ensure_api_key()

        cached_session = session_store.get_bot(bot_id)
        current_status = cached_session.get("status", "joining") if cached_session else "joining"
        meeting_url = cached_session.get("meeting_url") if cached_session else None

        url = f"{settings.baas_api_url}/{bot_id}"
        try:
            resp = requests.get(url, headers={"x-meeting-baas-api-key": self.api_key}, timeout=5)
            if resp.ok:
                res = resp.json()
                raw_data = res.get("data", res)
                current_status = raw_data.get("status", current_status)
        except (Timeout, ReqConnectionError, RequestException) as net_err:
            logger.debug(f"Upstream status check skipped/unavailable: {net_err}")

        status_data = BotStatusData(
            bot_id=bot_id,
            status=current_status,
            meeting_url=meeting_url
        )

        return BotStatusResponse(
            bot_id=bot_id,
            status=current_status,
            data=status_data
        )

    def leave_meeting(self, bot_id: str) -> BotLeaveResponse:
        self._ensure_api_key()
        
        # 1. Speaking Bots API requires a JSON body: {"bot_id": "..."}
        speaking_delete_url = f"{settings.speaking_api_url}/{bot_id}"
        logger.info(f"🛑 Sending leave request to Speaking Bots: {speaking_delete_url}")
        try:
            resp = requests.delete(
                speaking_delete_url,
                headers=self.headers,
                json={"bot_id": bot_id},
                timeout=10
            )
            if resp.ok or resp.status_code in (200, 202, 204):
                session_store.update_status(bot_id, "ended")
                logger.info(f"✅ Bot {bot_id} leave request successful via Speaking Bots API.")
                return BotLeaveResponse(
                    bot_id=bot_id,
                    message=f"Bot {bot_id} has left the meeting.",
                    success=True
                )
            else:
                logger.warning(f"Speaking Bots delete status {resp.status_code}: {resp.text}")
        except (Timeout, ReqConnectionError, RequestException) as net_err:
            logger.warning(f"Speaking Bots delete network issue, trying fallback: {net_err}")

        # 2. Fallback to standard baas API
        url = f"{settings.baas_api_url}/{bot_id}"
        try:
            resp = requests.delete(url, headers=self.headers, timeout=10)
            session_store.update_status(bot_id, "ended")
            logger.info(f"✅ Bot {bot_id} leave request completed.")
            return BotLeaveResponse(
                bot_id=bot_id,
                message=f"Bot {bot_id} has been instructed to leave the meeting.",
                success=True
            )
        except (Timeout, ReqConnectionError) as net_err:
            raise MeetingProviderNetworkError(f"Connection timeout sending leave request: {str(net_err)}")
        except RequestException as req_err:
            raise MeetingProviderNetworkError(f"HTTP error sending leave request: {str(req_err)}")

meeting_baas_service = MeetingBaaSClient()
