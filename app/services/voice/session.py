import asyncio
from typing import Dict, List, Optional
from fastapi import WebSocket
from app.core.config import settings
from app.services.meeting_memory import MeetingMemory

class VoiceSession:
    def __init__(self, session_id: str, bot_name: str, system_prompt: str, entry_message: str):
        self.session_id: str = session_id
        self.bot_name: str = bot_name
        self.system_prompt: str = system_prompt
        self.entry_message: str = entry_message

        # Audio & WebSocket channels
        self.in_websocket: Optional[WebSocket] = None
        self.out_websocket: Optional[WebSocket] = None

        # Rolling in-meeting memory (chronological transcript + Q&A history)
        self.memory: MeetingMemory = MeetingMemory(bot_name=bot_name)
        # Running notes (people, facts, todos, summary) kept up to date in the background
        from app.services.voice.memory_notes import NotesUpdater
        self.notes_updater: NotesUpdater = NotesUpdater(self)
        self.memory.on_append = self.notes_updater.notify
        self.last_bot_spoke_at: float = 0.0

        # Audio state
        self.sample_rate: int = settings.audio_sample_rate
        self.sample_width: int = settings.audio_sample_width
        self.channels: int = settings.audio_channels

        # VAD & Buffer state
        self.audio_buffer: bytearray = bytearray()
        self.is_speaking: bool = False       # True when bot is currently talking
        self.user_is_speaking: bool = False  # True when user speech is in progress
        self.silence_bytes: int = 0
        self.speech_bytes: int = 0
        self.frames_received: int = 0
        self.lock: asyncio.Lock = asyncio.Lock()
        # Serializes reply generation + playback so two turns never talk over each other
        self.turn_lock: asyncio.Lock = asyncio.Lock()
        # Serializes writes to the outbound audio socket (greeting vs. replies)
        self.speak_lock: asyncio.Lock = asyncio.Lock()

        # Provider bot_id (set once dispatch returns) and one-time greeting flag
        self.bot_id: Optional[str] = None
        self.greeted: bool = False

        # Speaker attribution from Meeting BaaS speaker events
        self.active_speaker: Optional[str] = None   # last human Meet reported as speaking
        self.segment_speaker: Optional[str] = None  # speaker captured for the current speech segment

        # Conversation mode: who the bot is currently talking with, and when they last spoke
        self.last_interlocutor: Optional[str] = None
        self.conversation_active_at: float = 0.0

        # Multi-party awareness from Meet speaker indicators
        self.participant_names: set[str] = set()       # everyone seen in the call (except this bot)
        self.speaking_since: Dict[str, float] = {}     # who is speaking right now -> since when
        self.speaking_last_seen: Dict[str, float] = {} # last time their speaking indicator was on
        self.ai_reply_streak: int = 0                  # consecutive replies to AI agents (loop guard)

        # Diagnostics
        self.turns_count: int = 0
        self.total_speech_secs: float = 0.0

    @property
    def conversation_history(self) -> List[Dict[str, str]]:
        return self.memory.qa_history
