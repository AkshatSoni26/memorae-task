import os
from typing import Dict, List, Optional
from fastapi import WebSocket
from groq import AsyncGroq
import httpx
from app.core.config import settings
from app.services.voice.addressing import (
    clean_for_speech,
    evaluate_addressing,
    fuzzy_vocative,
    is_ai_participant,
    looks_like_follow_up,
    name_variants,
)
from app.services.voice.audio_utils import calculate_rms
from app.services.voice.constants import (
    AI_REPLY_STREAK_MAX,
    BACKCHANNEL_PHRASES,
    BARGE_IN_SECS,
    BYTES_PER_SECOND,
    CHUNK_SIZE_BYTES,
    DEEPGRAM_LISTEN_URL,
    DEEPGRAM_SPEAK_URL,
    DEFAULT_FOLLOW_UP_WINDOW_SECS,
    DEFAULT_MAX_SPEECH_BYTES,
    DEFAULT_MIN_SPEECH_BYTES,
    DEFAULT_SILENCE_BYTES_TRIGGER,
    DEFAULT_SILENCE_THRESHOLD_RMS,
    FLOOR_WAIT_MAX_SECS,
    GROUP_ADDRESS_RE,
    LEADING_FILLERS_RE,
    QUESTION_STARTERS,
    SESSIONS_FILE,
    SPEAKING_GRACE_SECS,
    WHISPER_HALLUCINATIONS,
)
from app.services.voice.session import VoiceSession
from app.services.voice.session_manager import SessionManager
from app.services.voice.stt import (
    get_sibling_bot_names,
    get_stt_keyterms,
    get_stt_prompt,
    transcribe_deepgram,
)
from app.services.voice.stream_handler import (
    greet_when_admitted,
    handle_audio_in,
    handle_audio_out,
    handle_stream_event,
    wait_until_admitted,
)
from app.services.voice.turn_handler import _respond_to_turn, process_voice_turn
from app.services.voice.tts import (
    expire_speakers,
    others_speaking,
    produce_audio,
    speak_text,
    speak_text_locked,
    split_sentences,
    stream_deepgram,
    synthesize_groq,
    synthesize_pcm,
    wait_for_floor,
    warm_up_tts,
)

class GroqVoicePipelineManager:
    # Retain all class-level constants for compatibility
    BACKCHANNEL_PHRASES = BACKCHANNEL_PHRASES
    WHISPER_HALLUCINATIONS = WHISPER_HALLUCINATIONS
    QUESTION_STARTERS = QUESTION_STARTERS
    LEADING_FILLERS_RE = LEADING_FILLERS_RE
    GROUP_ADDRESS_RE = GROUP_ADDRESS_RE
    FLOOR_WAIT_MAX_SECS = FLOOR_WAIT_MAX_SECS
    BARGE_IN_SECS = BARGE_IN_SECS
    DEEPGRAM_SPEAK_URL = DEEPGRAM_SPEAK_URL
    DEEPGRAM_LISTEN_URL = DEEPGRAM_LISTEN_URL
    SPEAKING_GRACE_SECS = SPEAKING_GRACE_SECS
    AI_REPLY_STREAK_MAX = AI_REPLY_STREAK_MAX
    SESSIONS_FILE = SESSIONS_FILE

    def __init__(self):
        self._session_manager = SessionManager(on_session_created=self._warm_up_tts)
        self._http: Optional[httpx.AsyncClient] = None
        self.groq_client: Optional[AsyncGroq] = None

        # VAD Tuning Constants
        self.chunk_size_bytes: int = CHUNK_SIZE_BYTES
        self.bytes_per_second: int = BYTES_PER_SECOND
        self.silence_threshold_rms: float = DEFAULT_SILENCE_THRESHOLD_RMS
        self.silence_bytes_trigger: int = DEFAULT_SILENCE_BYTES_TRIGGER
        self.min_speech_bytes: int = DEFAULT_MIN_SPEECH_BYTES
        self.max_speech_bytes: int = DEFAULT_MAX_SPEECH_BYTES
        self.follow_up_window_secs: float = DEFAULT_FOLLOW_UP_WINDOW_SECS

    @property
    def sessions(self) -> Dict[str, VoiceSession]:
        return self._session_manager.sessions

    @property
    def known_ai_names(self) -> set[str]:
        return self._session_manager.known_ai_names

    def register_ai_participant(self, name: str) -> None:
        self._session_manager.register_ai_participant(name)

    def _get_client(self) -> AsyncGroq:
        if self.groq_client is None:
            api_key = settings.groq_api_key or os.getenv("GROQ_API_KEY", "")
            self.groq_client = AsyncGroq(api_key=api_key)
        return self.groq_client

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0),
                limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=300.0),
            )
        return self._http

    def create_session(
        self,
        session_id: str,
        bot_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        entry_message: Optional[str] = None
    ) -> VoiceSession:
        return self._session_manager.create_session(session_id, bot_name, system_prompt, entry_message)

    def _save_session_meta(self, session_id: str, meta: dict) -> None:
        self._session_manager.save_session_meta(session_id, meta)

    def restore_session(self, session_id: str) -> VoiceSession:
        return self._session_manager.restore_session(session_id)

    def get_session(self, session_id: str) -> Optional[VoiceSession]:
        return self._session_manager.get_session(session_id)

    def alias_session(self, alias_id: str, session_id: str) -> None:
        self._session_manager.alias_session(alias_id, session_id)

    def remove_session(self, key: str) -> None:
        self._session_manager.remove_session(key)

    def _looks_like_follow_up(self, text_lower: str) -> bool:
        return looks_like_follow_up(text_lower)

    def _name_variants(self, bot_name_lower: str) -> list[str]:
        return name_variants(bot_name_lower)

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        return clean_for_speech(text)

    def _fuzzy_vocative(self, words: list[str], bot_name_lower: str) -> Optional[str]:
        return fuzzy_vocative(words, bot_name_lower)

    def _sibling_bot_names(self, session: VoiceSession) -> list[str]:
        return get_sibling_bot_names(session, self._session_manager.get_all_sessions(), self.known_ai_names)

    def _is_ai_participant(self, session: VoiceSession, name: str) -> bool:
        return is_ai_participant(name, session.bot_name, self._sibling_bot_names(session))

    def _evaluate_addressing(self, session: VoiceSession, user_text: str, speaker: str = "Participant") -> tuple[bool, str]:
        return evaluate_addressing(session, user_text, speaker, self._sibling_bot_names(session), self.follow_up_window_secs)

    @staticmethod
    def _calculate_rms(chunk: bytes) -> float:
        return calculate_rms(chunk)

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        return split_sentences(text)

    def _stream_deepgram(self, text: str):
        return stream_deepgram(self._get_http(), text)

    async def _synthesize_groq(self, text: str) -> bytes:
        return await synthesize_groq(self._get_client(), text)

    async def _synthesize_pcm(self, text: str) -> bytes:
        return await synthesize_pcm(self._get_client(), self._get_http(), text)

    def _warm_up_tts(self) -> None:
        warm_up_tts(self._get_http())

    def _expire_speakers(self, session: VoiceSession, now: float) -> None:
        expire_speakers(session, now)

    def _others_speaking(self, session: VoiceSession, started_after: float = 0.0, min_secs: float = 0.0) -> list[str]:
        return others_speaking(session, started_after, min_secs)

    async def _wait_for_floor(self, session: VoiceSession) -> None:
        await wait_for_floor(session, self.FLOOR_WAIT_MAX_SECS)

    async def _produce_audio(self, text: str, queue) -> None:
        await produce_audio(self._get_client(), self._get_http(), text, queue)

    async def _speak_text_locked(self, session: VoiceSession, text: str) -> float:
        return await speak_text_locked(session, text, self._get_client(), self._get_http(), self.chunk_size_bytes)

    async def speak_text(self, session: VoiceSession, text: str) -> float:
        return await speak_text(session, text, self._get_client(), self._get_http(), self.chunk_size_bytes)

    def _stt_keyterms(self, session: VoiceSession) -> list[str]:
        return get_stt_keyterms(session, self._sibling_bot_names(session))

    def _stt_prompt(self, session: VoiceSession) -> str:
        return get_stt_prompt(session, self._sibling_bot_names(session))

    async def _transcribe_deepgram(self, session: VoiceSession, wav_payload: bytes) -> str:
        return await transcribe_deepgram(self._get_http(), session, wav_payload, self._sibling_bot_names(session))

    async def process_turn(self, session: VoiceSession, pcm_bytes: bytes, speaker: str = "Participant") -> None:
        await process_voice_turn(
            session=session,
            pcm_bytes=pcm_bytes,
            speaker=speaker,
            groq_client=self._get_client(),
            http_client=self._get_http(),
            all_sessions=self._session_manager.get_all_sessions(),
            known_ai_names=self.known_ai_names,
            follow_up_window_secs=self.follow_up_window_secs,
            chunk_size_bytes=self.chunk_size_bytes,
        )

    async def _respond_to_turn(
        self, session: VoiceSession, turn_num: int, user_text: str, stt_latency: float, turn_start: float,
        speaker: str = "Participant"
    ) -> None:
        await _respond_to_turn(
            session=session,
            turn_num=turn_num,
            user_text=user_text,
            stt_latency=stt_latency,
            turn_start=turn_start,
            speaker=speaker,
            groq_client=self._get_client(),
            http_client=self._get_http(),
            sibling_bots=self._sibling_bot_names(session),
            follow_up_window_secs=self.follow_up_window_secs,
            chunk_size_bytes=self.chunk_size_bytes,
        )

    def _handle_stream_event(self, session: VoiceSession, text_data: str) -> None:
        handle_stream_event(session, text_data, self._is_ai_participant)

    async def handle_audio_in(self, session: VoiceSession, websocket: WebSocket) -> None:
        await handle_audio_in(
            session=session,
            websocket=websocket,
            process_turn_fn=self.process_turn,
            is_ai_participant_fn=self._is_ai_participant,
            silence_threshold_rms=self.silence_threshold_rms,
            silence_bytes_trigger=self.silence_bytes_trigger,
            min_speech_bytes=self.min_speech_bytes,
            max_speech_bytes=self.max_speech_bytes,
        )

    async def _wait_until_admitted(self, session: VoiceSession, timeout_secs: float = 180.0) -> bool:
        return await wait_until_admitted(session, timeout_secs)

    async def _greet_when_admitted(self, session: VoiceSession) -> None:
        await greet_when_admitted(session, self._get_client(), self._get_http())

    async def handle_audio_out(self, session: VoiceSession, websocket: WebSocket) -> None:
        await handle_audio_out(session, websocket, self._get_client(), self._get_http())

groq_voice_pipeline = GroqVoicePipelineManager()

__all__ = ["VoiceSession", "GroqVoicePipelineManager", "groq_voice_pipeline"]
