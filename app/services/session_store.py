from typing import Dict, Optional
from app.models.bot import BotSession, BotStatusEnum
from app.core.logger import logger

class SessionStore:
    def __init__(self):
        self._sessions: Dict[str, dict] = {}

    def register_bot(self, bot_id: str, meeting_url: str, bot_name: str):
        self._sessions[bot_id] = {
            "bot_id": bot_id,
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "status": "joining"
        }
        logger.info(f"Registered session for bot {bot_id} (meeting: {meeting_url})")

    def update_status(self, bot_id: str, status_code: str):
        if bot_id in self._sessions:
            self._sessions[bot_id]["status"] = status_code
        else:
            self._sessions[bot_id] = {
                "bot_id": bot_id,
                "status": status_code
            }
        logger.info(f"Updated status for bot {bot_id} -> {status_code}")

    def get_bot(self, bot_id: str) -> Optional[dict]:
        return self._sessions.get(bot_id)

    def get_latest_bot(self) -> Optional[dict]:
        if not self._sessions:
            return None
        # Return most recently added or updated bot
        last_key = list(self._sessions.keys())[-1]
        return self._sessions[last_key]

session_store = SessionStore()
