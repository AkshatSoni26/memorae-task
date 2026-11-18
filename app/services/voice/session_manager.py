import json
import time
from typing import Callable, Dict, Optional
from app.core.config import settings
from app.core.logger import logger
from app.prompts import load_system_prompt
from app.services.voice.constants import SESSIONS_FILE
from app.services.voice.session import VoiceSession

class SessionManager:
    def __init__(self, on_session_created: Optional[Callable[[], None]] = None):
        self.sessions: Dict[str, VoiceSession] = {}
        self.known_ai_names: set[str] = set()
        self.on_session_created = on_session_created

    def register_ai_participant(self, name: str) -> None:
        """Marks a display name as an AI agent (used for bots added via Speaking Bots)."""
        if name and name.strip():
            self.known_ai_names.add(name.strip())  # original casing (used as STT keyterm)

    def save_session_meta(self, session_id: str, meta: dict) -> None:
        """Persists session metadata to disk for reconnect resilience across restarts."""
        try:
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8")) if SESSIONS_FILE.exists() else {}
            data[session_id] = {**data.get(session_id, {}), **meta, "saved_at": time.time()}
            # keep only the last day of sessions
            data = {k: v for k, v in data.items() if time.time() - v.get("saved_at", 0) < 86400}
            SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as save_err:
            logger.warning(f"⚠️ Could not save voice session meta: {save_err}")

    def create_session(
        self,
        session_id: str,
        bot_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        entry_message: Optional[str] = None
    ) -> VoiceSession:
        name = bot_name or settings.default_bot_name
        prompt = system_prompt or load_system_prompt(bot_name=name)
        greeting = entry_message or settings.default_entry_message

        session = VoiceSession(
            session_id=session_id,
            bot_name=name,
            system_prompt=prompt,
            entry_message=greeting
        )
        self.sessions[session_id] = session
        logger.info(f"🎙️ Created Groq voice pipeline session: {session_id} for bot '{name}'")
        if settings.speech_provider == "deepgram":
            engines = f"STT: Deepgram {settings.deepgram_stt_model} | TTS: Deepgram {settings.deepgram_tts_model}"
        else:
            engines = f"STT: Groq {settings.groq_stt_model} | TTS: Groq {settings.groq_tts_model} ({settings.groq_tts_voice})"
        print(f"🎛️ [ENGINES] USE_GROQ={settings.use_groq} -> {engines} | LLM: Groq {settings.groq_llm_model}", flush=True)

        if bot_name:
            self.save_session_meta(session_id, {"bot_name": name, "entry_message": entry_message})

        if self.on_session_created:
            self.on_session_created()
        return session

    def restore_session(self, session_id: str) -> VoiceSession:
        """Rebuilds a session after a server restart (same name / greeting); never re-greets."""
        meta: dict = {}
        try:
            if SESSIONS_FILE.exists():
                meta = json.loads(SESSIONS_FILE.read_text(encoding="utf-8")).get(session_id, {})
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as load_err:
            logger.warning(f"⚠️ Could not read voice session meta: {load_err}")

        session = self.create_session(
            session_id,
            bot_name=meta.get("bot_name"),
            entry_message=meta.get("entry_message"),
        )
        if meta.get("bot_id"):
            self.sessions[meta["bot_id"]] = session
            session.bot_id = meta["bot_id"]
        session.greeted = True
        print(
            f"♻️ [RESTORE] Session {session_id} reconnected after restart as "
            f"'{session.bot_name}'{'' if meta else ' (no saved name - using default)'}",
            flush=True,
        )
        return session

    def get_session(self, session_id: str) -> Optional[VoiceSession]:
        return self.sessions.get(session_id)

    def alias_session(self, alias_id: str, session_id: str) -> None:
        """Makes a session reachable by a second key (e.g. the provider bot_id)."""
        if session_id in self.sessions:
            self.sessions[alias_id] = self.sessions[session_id]
            self.sessions[session_id].bot_id = alias_id
            self.save_session_meta(session_id, {"bot_id": alias_id})

    def remove_session(self, key: str) -> None:
        """Drops a session and every alias pointing at it."""
        session = self.sessions.get(key)
        if session is None:
            return
        for k in [k for k, v in self.sessions.items() if v is session]:
            del self.sessions[k]
        logger.info(f"🧹 Removed voice session {session.session_id}")

    def get_all_sessions(self) -> list[VoiceSession]:
        return list(self.sessions.values())
