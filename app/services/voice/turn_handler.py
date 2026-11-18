import asyncio
import re
import time
from typing import Callable, Optional
from groq import AsyncGroq
import httpx
from app.core.config import settings
from app.core.logger import logger
from app.services.voice.addressing import evaluate_addressing
from app.services.voice.audio_utils import pcm_to_wav, resample_pcm_16k
from app.services.voice.constants import WHISPER_HALLUCINATIONS
from app.services.voice.llm import query_llm
from app.services.voice.session import VoiceSession
from app.services.voice.stt import get_sibling_bot_names, transcribe_speech
from app.services.voice.tts import speak_text

async def process_voice_turn(*args, **kwargs) -> None:
    """Runs one voice turn. Turns are fire-and-forget tasks, so any failure is logged here
    instead of vanishing as "Task exception was never retrieved"."""
    try:
        await _process_voice_turn(*args, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as turn_err:
        logger.exception(f"❌ Voice turn crashed: {type(turn_err).__name__}: {turn_err}")
        print(f"❌ [TURN ERROR] {type(turn_err).__name__}: {turn_err}", flush=True)

async def _process_voice_turn(
    session: VoiceSession,
    pcm_bytes: bytes,
    speaker: str,
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
    all_sessions: list[VoiceSession],
    known_ai_names: set[str],
    follow_up_window_secs: float = 30.0,
    chunk_size_bytes: int = 960,
) -> None:
    """
    Executes full voice turn:
    1. STT: Deepgram Nova-3, or Groq Whisper when USE_GROQ=true
    2. Address check / Conversation context
    3. Groq Qwen LLM (qwen/qwen3.8-27b)
    4. TTS: Deepgram Flux (streamed), or Groq Orpheus when USE_GROQ=true
    5. Real-time audio stream to Google Meet
    """
    session.turns_count += 1
    turn_num = session.turns_count
    turn_start = time.time()

    # 1. Downsample 24kHz to 16kHz for STT efficiency
    stt_pcm, stt_rate = resample_pcm_16k(
        pcm_bytes,
        in_rate=session.sample_rate,
        sample_width=session.sample_width,
        channels=session.channels,
    )
    wav_payload = pcm_to_wav(
        stt_pcm,
        sample_rate=stt_rate,
        sample_width=session.sample_width,
        channels=session.channels,
    )

    # 2. Speech-to-text
    stt_start = time.time()
    logger.info(f"🎙️ [Turn #{turn_num}] Sending {len(wav_payload):,} audio bytes to STT...")
    stt_engine = settings.speech_provider
    sibling_bots = get_sibling_bot_names(session, all_sessions, known_ai_names)

    user_text = await transcribe_speech(
        session=session,
        wav_payload=wav_payload,
        sibling_bots=sibling_bots,
        groq_client=groq_client,
        http_client=http_client,
    )
    if user_text is None:
        return  # STT error already logged
    if not user_text:
        print(f"🔇 [Turn #{turn_num}] STT returned no words ({time.time() - stt_start:.2f}s) - ignored.", flush=True)
        return

    stt_latency = time.time() - stt_start
    label = "DEEPGRAM NOVA-3 STT" if stt_engine == "deepgram" else "GROQ WHISPER STT"
    logger.info(f"📝 [Turn #{turn_num}] User said ({stt_latency:.2f}s, {stt_engine}): \"{user_text}\"")
    print("\n" + "=" * 60, flush=True)
    print(f"🎙️ [LIVE TURN #{turn_num}]", flush=True)
    print(f"📝 [{label}] {speaker}: \"{user_text}\" ({stt_latency:.2f}s)", flush=True)

    normalized = re.sub(r"[^a-z ]", "", user_text.lower()).strip()
    if len(normalized) < 2 or normalized in WHISPER_HALLUCINATIONS:
        print(f"🔇 [Turn #{turn_num}] Ignored noise / Whisper hallucination: \"{user_text}\"", flush=True)
        return

    # Steps 3-5 run under turn_lock to prevent audio overlap
    if session.turn_lock.locked():
        print(f"⏳ [Turn #{turn_num}] Waiting for previous reply to finish...", flush=True)
    async with session.turn_lock:
        await _respond_to_turn(
            session=session,
            turn_num=turn_num,
            user_text=user_text,
            stt_latency=stt_latency,
            turn_start=turn_start,
            speaker=speaker,
            groq_client=groq_client,
            http_client=http_client,
            sibling_bots=sibling_bots,
            follow_up_window_secs=follow_up_window_secs,
            chunk_size_bytes=chunk_size_bytes,
        )

async def _respond_to_turn(
    session: VoiceSession,
    turn_num: int,
    user_text: str,
    stt_latency: float,
    turn_start: float,
    speaker: str,
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
    sibling_bots: list[str],
    follow_up_window_secs: float,
    chunk_size_bytes: int,
) -> None:
    # 3. Multi-bot safe addressing gate
    is_addressed, gate_reason = evaluate_addressing(
        session=session,
        user_text=user_text,
        speaker=speaker,
        sibling_bot_names=sibling_bots,
        follow_up_window_secs=follow_up_window_secs,
    )

    if not is_addressed:
        session.memory.add_room_utterance(user_text, speaker=speaker)
        logger.info(
            f"🤫 [Turn #{turn_num}] Staying silent ({gate_reason}) "
            f"[memory: {session.memory.utterance_count} utterances]: \"{user_text}\""
        )
        print(
            f"🤫 [ADDRESSING GATE] Staying silent ({gate_reason}) "
            f"| 🧠 Memory: {session.memory.utterance_count} utterances",
            flush=True
        )
        print("=" * 60 + "\n", flush=True)
        return

    logger.info(f"🎯 [Turn #{turn_num}] Addressing gate passed ({gate_reason})")
    print(f"🎯 [ADDRESSING GATE] Engaged ({gate_reason})", flush=True)

    # 4. Groq LLM Generation
    bot_reply, llm_latency = await query_llm(
        groq_client=groq_client,
        session=session,
        user_text=user_text,
        speaker=speaker,
        turn_num=turn_num,
        gate_reason=gate_reason,
    )
    if not bot_reply:
        return

    logger.info(f"💡 [Turn #{turn_num}] Groq LLM reply ({llm_latency:.2f}s): \"{bot_reply}\"")
    print(f"🧠 [GROQ QWEN LLM]   {session.bot_name}: \"{bot_reply}\" ({llm_latency:.2f}s)", flush=True)

    session.memory.add_exchange(user_text, bot_reply, speaker=speaker)
    session.ai_reply_streak = session.ai_reply_streak + 1 if gate_reason == "ai_direct_address" else 0
    print(
        f"🗂️ [MEETING MEMORY]  {session.memory.utterance_count} rolling utterances | "
        f"{len(session.memory.qa_history)} Q&A msgs",
        flush=True
    )

    # 5. TTS Playback
    tts_latency = await speak_text(session, bot_reply, groq_client, http_client, chunk_size_bytes)
    session.last_bot_spoke_at = time.time()
    session.last_interlocutor = speaker
    session.conversation_active_at = session.last_bot_spoke_at

    first_audio_latency = stt_latency + llm_latency + tts_latency
    total_turn_time = time.time() - turn_start
    logger.info(
        f"⚡ [Turn #{turn_num}] Complete! First Audio: {first_audio_latency:.2f}s "
        f"(STT: {stt_latency:.2f}s | LLM: {llm_latency:.2f}s | TTS: {tts_latency:.2f}s) | Total incl. playback: {total_turn_time:.2f}s"
    )
    print(
        f"⚡ [LATENCY BREAKDOWN] First Audio: {first_audio_latency:.2f}s "
        f"(STT: {stt_latency:.2f}s | LLM: {llm_latency:.2f}s | TTS: {tts_latency:.2f}s)",
        flush=True
    )
    print("=" * 60 + "\n", flush=True)
