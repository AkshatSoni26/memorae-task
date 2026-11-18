import os
import requests
import uuid
from requests.exceptions import (
    Timeout,
    ConnectionError as ReqConnectionError,
    RequestException
)
from app.core.config import settings
from app.core.logger import logger
from app.core.exceptions import (
    MeetingServiceError,
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
from app.prompts import load_system_prompt
from app.services.session_store import session_store
from app.services.groq_voice_pipeline import groq_voice_pipeline

def get_discovered_webhook_url() -> str | None:
    """
    Dynamically discovers the current active public webhook endpoint:
    1. Checks explicit WEBHOOK_URL / PUBLIC_URL environment variable if set.
    2. Queries live ngrok local API (http://127.0.0.1:4040/api/tunnels) for the currently running tunnel.
    3. Returns None if no public endpoint is running (never uses stale hardcoded domains).
    """
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
            resp = requests.get(
                f"http://{host}:4040/api/tunnels",
                headers={"Host": "localhost:4040"},
                timeout=1.5
            )
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

def get_discovered_ws_url() -> str | None:
    """
    Dynamically discovers the current active public WebSocket base URL (wss://...).
    """
    # 1. Environment variable override
    env_url = (
        os.getenv("WS_URL")
        or os.getenv("PUBLIC_URL")
        or os.getenv("WEBHOOK_URL")
        or getattr(settings, "webhook_url", "")
    )
    if env_url and env_url.strip():
        cleaned = env_url.strip().rstrip("/")
        if cleaned.startswith("http://"):
            cleaned = "ws://" + cleaned[7:]
        elif cleaned.startswith("https://"):
            cleaned = "wss://" + cleaned[8:]
        elif not (cleaned.startswith("ws://") or cleaned.startswith("wss://")):
            cleaned = f"wss://{cleaned}"
        if cleaned.endswith("/webhook"):
            cleaned = cleaned[:-8]
        return cleaned

    # 2. Dynamic ngrok auto-discovery
    for host in ["127.0.0.1", "localhost"]:
        try:
            resp = requests.get(
                f"http://{host}:4040/api/tunnels",
                headers={"Host": "localhost:4040"},
                timeout=1.5
            )
            if resp.ok:
                tunnels = resp.json().get("tunnels", [])
                for tunnel in tunnels:
                    if tunnel.get("proto") == "https":
                        public_url = tunnel.get("public_url", "").rstrip("/")
                        if public_url:
                            wss_url = public_url.replace("https://", "wss://")
                            logger.info(f"🌐 Dynamically discovered active ngrok WSS: {wss_url}")
                            return wss_url
        except Exception:
            pass

    return None


MEETING_BAAS_V2_BOTS_URL = "https://api.meetingbaas.com/v2/bots"


def _provider_error_detail(resp: requests.Response) -> str:
    """Extracts a human-readable error message from a Meeting BaaS error response."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("error") or body.get("detail") or body)
    except ValueError:
        pass
    return resp.text[:500] or resp.reason


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

    def _post(self, url: str, payload: dict) -> requests.Response:
        try:
            return requests.post(url, json=payload, headers=self.headers, timeout=15)
        except (Timeout, ReqConnectionError) as net_err:
            logger.error(f"❌ Network failure calling {url}: {net_err}")
            raise MeetingProviderNetworkError(f"Could not reach Meeting BaaS ({url}): {net_err}")
        except RequestException as req_err:
            logger.error(f"❌ HTTP request error calling {url}: {req_err}")
            raise MeetingProviderNetworkError(f"HTTP request error: {req_err}")

    # ------------------------------------------------------------------ dispatch

    def dispatch_bot(self, request: JoinMeetingRequest) -> BotResponseData:
        """
        Dispatches exactly the engine that was requested. No silent fallbacks: if the chosen
        engine fails, the error is surfaced to the caller/UI instead of sending a different
        (possibly mute) bot into the meeting.
        """
        self._ensure_api_key()
        cleaned_url: str = sanitize_meeting_url(str(request.meeting_url))

        def clean_val(val: str | None, fallback: str) -> str:
            if not val or val.strip().lower() == "string":
                return fallback
            return val.strip()

        bot_name: str = clean_val(request.bot_name, settings.default_bot_name)
        entry_message: str = clean_val(request.entry_message, settings.default_entry_message)
        custom_prompt: str = clean_val(request.prompt, "")

        callback_url: str | None
        if request.webhook_url and str(request.webhook_url).lower() != "https://example.com/":
            callback_url = str(request.webhook_url)
        else:
            callback_url = get_discovered_webhook_url()

        pipeline_mode = (request.pipeline_provider or "groq").lower().strip()
        logger.info(f"🚀 Dispatching '{bot_name}' via engine '{pipeline_mode}' to meeting: {cleaned_url}")

        if pipeline_mode == "groq":
            return self._dispatch_groq(cleaned_url, bot_name, entry_message, custom_prompt, callback_url)
        if pipeline_mode in ("speaking_bots", "speaking", "hosted"):
            return self._dispatch_speaking_bot(
                cleaned_url, bot_name, entry_message, custom_prompt, callback_url, request.wake_phrases
            )
        raise MeetingServiceError(
            f"Unknown pipeline_provider '{request.pipeline_provider}'. Use 'groq' or 'speaking_bots'.",
            status_code=400
        )

    def _dispatch_groq(
        self, meeting_url: str, bot_name: str, entry_message: str, custom_prompt: str, callback_url: str | None
    ) -> BotResponseData:
        """Custom pipeline: Meeting BaaS v2 bidirectional audio streaming + Groq STT/LLM/TTS."""
        if not (settings.groq_api_key or os.getenv("GROQ_API_KEY")):
            raise MeetingConfigurationError("GROQ_API_KEY is not configured in .env (required for the Groq engine)")

        ws_base = get_discovered_ws_url()
        if not ws_base:
            raise MeetingConfigurationError(
                "No public WSS endpoint found. Start `ngrok http 8000` (or set PUBLIC_URL) - "
                "Meeting BaaS cannot stream audio to localhost."
            )

        session_id = str(uuid.uuid4())
        system_prompt = custom_prompt or load_system_prompt(bot_name=bot_name, include_silence_token=True)
        groq_voice_pipeline.create_session(
            session_id=session_id,
            bot_name=bot_name,
            system_prompt=system_prompt,
            entry_message=entry_message
        )

        output_url = f"{ws_base}/streaming/ws/audio-in/{session_id}"
        input_url = f"{ws_base}/streaming/ws/audio-out/{session_id}"
        logger.info(f"🎙️ Groq pipeline WebSockets:\n   • output_url: {output_url}\n   • input_url:  {input_url}")

        payload: dict = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "allow_multiple_bots": True,
            "ignored_participant_names": ["Memorae", "Boardy"],
            "streaming_enabled": True,
            "streaming_config": {
                "mode": "audio",
                "output_url": output_url,
                "input_url": input_url,
                "audio_frequency": 24000
            }
        }
        if callback_url:
            payload["callback_enabled"] = True
            payload["callback_config"] = {"url": callback_url, "method": "POST"}

        logger.info(f"📡 Dispatching bot via Meeting BaaS v2 streaming: {MEETING_BAAS_V2_BOTS_URL}")
        try:
            resp = self._post(MEETING_BAAS_V2_BOTS_URL, payload)
        except MeetingServiceError:
            groq_voice_pipeline.remove_session(session_id)
            raise
        if not resp.ok:
            groq_voice_pipeline.remove_session(session_id)
            detail = _provider_error_detail(resp)
            logger.error(f"❌ Meeting BaaS v2 dispatch failed ({resp.status_code}): {detail}")
            raise MeetingProviderResponseError(status_code=resp.status_code, detail=detail)

        resp_json = resp.json()
        bot_id_val = (resp_json.get("data") or {}).get("bot_id") or resp_json.get("bot_id")
        if not bot_id_val:
            groq_voice_pipeline.remove_session(session_id)
            raise MeetingProviderResponseError(status_code=502, detail=f"No bot_id in provider response: {resp_json}")
        bot_id_val = str(bot_id_val)

        session_store.register_bot(bot_id_val, meeting_url, bot_name, engine="groq", voice_session_id=session_id)
        groq_voice_pipeline.alias_session(bot_id_val, session_id)

        logger.info(f"✅ Bot '{bot_name}' dispatched with Groq Voice Pipeline. Bot ID: {bot_id_val}")
        return BotResponseData(
            bot_id=bot_id_val,
            status=BotStatusEnum.JOINING,
            provider_endpoint=MEETING_BAAS_V2_BOTS_URL,
            message=f"Bot '{bot_name}' dispatched with Groq Voice Pipeline (Whisper + Qwen + Orpheus)."
        )

    def _dispatch_speaking_bot(
        self, meeting_url: str, bot_name: str, entry_message: str, custom_prompt: str, callback_url: str | None,
        wake_phrases: list[str] | None = None
    ) -> BotResponseData:
        """Hosted fallback: Meeting BaaS Speaking Bots (Deepgram + GPT-4o-mini + Cartesia)."""
        # Our own Groq bots must treat this one as another AI agent (not a human)
        groq_voice_pipeline.register_ai_participant(bot_name)
        prompt = custom_prompt or load_system_prompt(bot_name=bot_name, include_silence_token=False)
        payload: dict = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "prompt": prompt,
            "personas": ["helpful_assistant"],
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "entry_message": entry_message,
            "enable_tools": False,
            # 'mentioned': only replies shortly after someone says its name (multi-party mode).
            "respond_mode": "mentioned",
            "turn_config": {
                "stop_secs": 0.4,
                "unfinished_pause_secs": 0.8
            },
            "briefing": {}
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if wake_phrases:
            payload["wake_phrases"] = [p.strip() for p in wake_phrases if p and p.strip()]

        logger.info(f"📡 Sending request to Speaking Bots API: {settings.speaking_api_url}")
        resp = self._post(settings.speaking_api_url, payload)
        if not resp.ok:
            detail = _provider_error_detail(resp)
            logger.error(f"❌ Speaking Bots dispatch failed ({resp.status_code}): {detail}")
            raise MeetingProviderResponseError(status_code=resp.status_code, detail=detail)

        resp_json = resp.json()
        bot_id_val = resp_json.get("bot_id") or resp_json.get("id") or (resp_json.get("data") or {}).get("bot_id")
        if not bot_id_val:
            raise MeetingProviderResponseError(status_code=502, detail=f"No bot_id in provider response: {resp_json}")
        bot_id_val = str(bot_id_val)

        session_store.register_bot(bot_id_val, meeting_url, bot_name, engine="speaking_bots")
        logger.info(f"✅ Bot '{bot_name}' dispatched via Speaking Bots API. Bot ID: {bot_id_val}")
        return BotResponseData(
            bot_id=bot_id_val,
            status=BotStatusEnum.JOINING,
            provider_endpoint=settings.speaking_api_url,
            message=f"Bot '{bot_name}' dispatched via hosted Speaking Bots API."
        )

    # ------------------------------------------------------------------ status

    def get_bot_usage(self, bot_id: str) -> dict | None:
        """Retrieves live Speaking Bots pipeline metrics (audio seconds, turns, TTS plays)."""
        url = f"{settings.speaking_api_url}/{bot_id}/usage"
        try:
            resp = requests.get(url, headers=self.headers, timeout=5)
            if resp.ok:
                return resp.json()
        except Exception:
            pass
        return None

    def get_status(self, bot_id: str) -> BotStatusResponse:
        """
        Returns the normalized app status (creating / joining / waiting_for_admission /
        in_call / ended / failed). Webhooks are the primary source (they carry the waiting-room
        state); for Groq/v2 bots the v2 status endpoint is polled as a backup.
        """
        self._ensure_api_key()
        cached = session_store.get_bot(bot_id)
        if cached is None:
            # Unknown locally (e.g. server restarted mid-call): adopt it as a v2 bot and ask upstream.
            cached = session_store.adopt_bot(bot_id, engine="groq")

        if cached.get("engine") == "groq" and cached.get("status") not in ("ended", "failed"):
            url = f"{MEETING_BAAS_V2_BOTS_URL}/{bot_id}/status"
            try:
                resp = requests.get(url, headers={"x-meeting-baas-api-key": self.api_key}, timeout=5)
                if resp.ok:
                    raw = (resp.json().get("data") or {}).get("status")
                    session_store.update_status(bot_id, raw)
                else:
                    logger.debug(f"v2 status check returned {resp.status_code}: {resp.text[:200]}")
            except RequestException as net_err:
                logger.debug(f"Upstream status check unavailable: {net_err}")

        cached = session_store.get_bot(bot_id) or {}
        current_status = cached.get("status", "joining")
        extra: dict[str, str] = {"engine": str(cached.get("engine") or "")}
        if cached.get("raw_status"):
            extra["provider_status"] = str(cached["raw_status"])
        if cached.get("error"):
            extra["error"] = str(cached["error"])

        return BotStatusResponse(
            bot_id=bot_id,
            status=current_status,
            data=BotStatusData(bot_id=bot_id, status=current_status, meeting_url=cached.get("meeting_url")),
            extra_details=extra
        )

    # ------------------------------------------------------------------ leave

    def leave_meeting(self, bot_id: str) -> BotLeaveResponse:
        """Removes the bot using the endpoint that matches the engine it was created with."""
        self._ensure_api_key()
        cached = session_store.get_bot(bot_id) or {}
        engine = cached.get("engine", "groq")

        try:
            if engine == "speaking_bots":
                url = f"{settings.speaking_api_url}/{bot_id}"
                logger.info(f"🛑 Removing Speaking Bot: DELETE {url}")
                resp = requests.delete(url, headers=self.headers, json={"bot_id": bot_id}, timeout=10)
            else:
                url = f"{MEETING_BAAS_V2_BOTS_URL}/{bot_id}/leave"
                logger.info(f"🛑 Removing v2 bot: POST {url}")
                resp = requests.post(url, headers={"x-meeting-baas-api-key": self.api_key}, timeout=10)
        except (Timeout, ReqConnectionError) as net_err:
            raise MeetingProviderNetworkError(f"Connection timeout sending leave request: {net_err}")
        except RequestException as req_err:
            raise MeetingProviderNetworkError(f"HTTP error sending leave request: {req_err}")

        if not resp.ok:
            detail = _provider_error_detail(resp)
            logger.error(f"❌ Leave request failed for bot {bot_id} ({resp.status_code}): {detail}")
            raise MeetingProviderResponseError(status_code=resp.status_code, detail=detail)

        session_store.update_status(bot_id, "ended")
        if cached.get("voice_session_id"):
            groq_voice_pipeline.remove_session(cached["voice_session_id"])
        groq_voice_pipeline.remove_session(bot_id)
        logger.info(f"✅ Bot {bot_id} left the meeting.")
        return BotLeaveResponse(bot_id=bot_id, message=f"Bot {bot_id} has left the meeting.", success=True)

meeting_baas_service = MeetingBaaSClient()
