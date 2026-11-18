from typing import Dict, Optional
from app.core.logger import logger

# Application-level statuses (PRD §7), ordered by lifecycle progression.
STATUS_RANK: Dict[str, int] = {
    "creating": 0,
    "joining": 1,
    "waiting_for_admission": 2,
    "in_call": 3,
    "ended": 4,
    "failed": 4,
}

# Raw provider codes (v2 status API, v2 webhooks, Speaking Bots events) -> app status.
_PROVIDER_STATUS_MAP: Dict[str, str] = {
    # creating
    "queued": "creating",
    "created": "creating",
    "creating": "creating",
    # joining
    "joining": "joining",
    "joining_call": "joining",
    "retrying": "joining",
    "starting": "joining",
    # waiting for admission
    "in_waiting_room": "waiting_for_admission",
    "waiting_room": "waiting_for_admission",
    "waiting_for_admission": "waiting_for_admission",
    # in call
    "in_call": "in_call",
    "in_call_recording": "in_call",
    "in_call_not_recording": "in_call",
    "recording_paused": "in_call",
    "recording_resumed": "in_call",
    "active": "in_call",
    "running": "in_call",
    # ended
    "call_ended": "ended",
    "ended": "ended",
    "completed": "ended",
    "transcribing": "ended",
    "bot_removed": "ended",
    "bot_left": "ended",
    "left": "ended",
    "stopped": "ended",
    "bot.completed": "ended",
    # failed
    "failed": "failed",
    "fatal": "failed",
    "error": "failed",
    "bot_rejected": "failed",
    "bot_not_accepted": "failed",
    "waiting_room_timeout": "failed",
    "timeout_waiting_to_start": "failed",
    "meeting_not_found": "failed",
    "invalid_meeting_url": "failed",
    "bot.failed": "failed",
}


def normalize_status(raw: Optional[str]) -> Optional[str]:
    """Maps any provider status/event code to one of the app statuses (or None if unknown)."""
    if not raw:
        return None
    return _PROVIDER_STATUS_MAP.get(str(raw).strip().lower())


class SessionStore:
    def __init__(self):
        self._sessions: Dict[str, dict] = {}

    def register_bot(self, bot_id: str, meeting_url: str, bot_name: str, engine: str, voice_session_id: Optional[str] = None):
        self._sessions[bot_id] = {
            "bot_id": bot_id,
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "engine": engine,  # "groq" (v2 streaming) or "speaking_bots"
            "voice_session_id": voice_session_id,
            "status": "joining",
            "raw_status": None,
            "error": None,
        }
        logger.info(f"Registered session for bot {bot_id} (engine: {engine}, meeting: {meeting_url})")

    def update_status(self, bot_id: str, raw_status: Optional[str], error: Optional[str] = None) -> Optional[str]:
        """
        Records a provider status. Statuses only move forward (a late 'joining' poll never
        overwrites 'waiting_for_admission' or 'in_call'); terminal states always win.
        Returns the resulting app status.
        """
        session = self._sessions.setdefault(bot_id, {"bot_id": bot_id, "status": "joining"})
        app_status = normalize_status(raw_status)
        if raw_status:
            session["raw_status"] = raw_status
        if error:
            session["error"] = error
        if app_status is None:
            logger.debug(f"Unmapped provider status for bot {bot_id}: {raw_status}")
            return session.get("status")

        current = session.get("status", "joining")
        if current in ("ended", "failed"):
            return current  # terminal: never resurrect
        if STATUS_RANK[app_status] >= STATUS_RANK.get(current, 0):
            if current != app_status:
                logger.info(f"Updated status for bot {bot_id}: {current} -> {app_status} (provider: {raw_status})")
            session["status"] = app_status
        return session["status"]

    def adopt_bot(self, bot_id: str, engine: str) -> dict:
        """Tracks a bot this process didn't dispatch (e.g. after a server restart mid-call)."""
        session = self._sessions.setdefault(bot_id, {"bot_id": bot_id, "status": "joining"})
        session.setdefault("engine", engine)
        return session

    def get_bot(self, bot_id: str) -> Optional[dict]:
        return self._sessions.get(bot_id)

    def get_latest_bot(self) -> Optional[dict]:
        if not self._sessions:
            return None
        last_key = list(self._sessions.keys())[-1]
        return self._sessions[last_key]

session_store = SessionStore()
