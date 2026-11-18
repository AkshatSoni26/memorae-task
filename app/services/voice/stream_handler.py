import asyncio
import json
import time
from typing import Callable, Coroutine, Optional
from fastapi import WebSocket, WebSocketDisconnect
from groq import AsyncGroq
import httpx
from app.core.config import settings
from app.core.logger import logger
from app.services.voice.audio_utils import calculate_rms
from app.services.voice.constants import (
    DEFAULT_MAX_SPEECH_BYTES,
    DEFAULT_MIN_SPEECH_BYTES,
    DEFAULT_SILENCE_BYTES_TRIGGER,
    DEFAULT_SILENCE_THRESHOLD_RMS,
    SPEAKING_GRACE_SECS,
)
from app.services.voice.session import VoiceSession
from app.services.voice.tts import expire_speakers, speak_text

def handle_stream_event(
    session: VoiceSession,
    text_data: str,
    is_ai_participant_fn: Callable[[VoiceSession, str], bool]
) -> None:
    """
    Parses JSON text frames on the audio-in socket:
    - handshake dict ({"protocol_version", "bot_id", ...}) -> remember bot_id
    - speaker list ([{"name", "isSpeaking", ...}, ...]) -> track the active human speaker
    """
    try:
        payload = json.loads(text_data)
    except (json.JSONDecodeError, ValueError):
        print(f"ℹ️ [AUDIO-IN TEXT] {text_data[:120]}", flush=True)
        return

    if isinstance(payload, dict):
        if payload.get("bot_id") and not session.bot_id:
            session.bot_id = str(payload["bot_id"])
        print(f"ℹ️ [AUDIO-IN HANDSHAKE] {text_data[:160]}", flush=True)
        return

    if isinstance(payload, list):
        bot_name_lower = session.bot_name.lower().strip()
        now = time.time()
        speaking_now: set[str] = set()
        for participant in payload:
            if not isinstance(participant, dict):
                continue
            name = str(participant.get("name") or "").strip()
            if not name or name.lower() == bot_name_lower:
                continue
            session.participant_names.add(name)
            if participant.get("isSpeaking", participant.get("is_speaking")):
                speaking_now.add(name)

        for name in speaking_now:
            last_seen = session.speaking_last_seen.get(name)
            if name not in session.speaking_since or last_seen is None or now - last_seen > SPEAKING_GRACE_SECS:
                session.speaking_since[name] = now
                tag = " (AI)" if is_ai_participant_fn(session, name) else ""
                print(f"🗣️ [SPEAKER] {name}{tag} is speaking", flush=True)
            session.speaking_last_seen[name] = now
        expire_speakers(session, now)

        if speaking_now:
            newest = max(speaking_now, key=lambda n: session.speaking_since[n])
            session.active_speaker = newest
            if session.user_is_speaking and not session.segment_speaker:
                session.segment_speaker = newest

async def handle_audio_in(
    session: VoiceSession,
    websocket: WebSocket,
    process_turn_fn: Callable[[VoiceSession, bytes, str], Coroutine],
    is_ai_participant_fn: Callable[[VoiceSession, str], bool],
    silence_threshold_rms: float = DEFAULT_SILENCE_THRESHOLD_RMS,
    silence_bytes_trigger: int = DEFAULT_SILENCE_BYTES_TRIGGER,
    min_speech_bytes: int = DEFAULT_MIN_SPEECH_BYTES,
    max_speech_bytes: int = DEFAULT_MAX_SPEECH_BYTES,
) -> None:
    """
    Receives 24kHz raw PCM audio from Meeting BaaS output_url, runs VAD,
    and triggers turn execution on speech pause.
    """
    session.in_websocket = websocket
    logger.info(f"🎧 Inbound audio stream connected for session {session.session_id}")
    print(f"\n🎧 [TERMINAL] Google Meet Audio Stream Connected! Session: {session.session_id}", flush=True)
    hb_started, hb_frames, hb_peak = time.time(), 0, 0.0

    try:
        while True:
            message = await websocket.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                break

            data = message.get("bytes")
            if not data:
                text_data = message.get("text")
                if text_data:
                    handle_stream_event(session, text_data, is_ai_participant_fn)
                continue

            session.frames_received += 1
            rms = calculate_rms(data)
            if session.frames_received == 1:
                print(f"🎵 [TERMINAL] First audio packet received from Google Meet! ({len(data)} bytes, RMS: {rms:.0f})", flush=True)

            hb_frames += 1
            hb_peak = max(hb_peak, rms)
            if time.time() - hb_started >= 10.0:
                print(
                    f"🎚️ [AUDIO-IN] {hb_frames} packets in last 10s | peak RMS {hb_peak:.0f} "
                    f"(speech threshold {silence_threshold_rms:.0f}) | bot speaking: {session.is_speaking} "
                    f"| output socket: {'OK' if session.out_websocket else 'MISSING'}",
                    flush=True
                )
                hb_started, hb_frames, hb_peak = time.time(), 0, 0.0

            if session.is_speaking:
                continue

            is_speech = rms > silence_threshold_rms

            async with session.lock:
                if is_speech:
                    if not session.user_is_speaking:
                        print(f"👂 [TERMINAL] Hearing speech in Google Meet... (RMS: {rms:.0f})", flush=True)
                        session.segment_speaker = None
                    session.speech_bytes += len(data)
                    session.silence_bytes = 0
                    session.user_is_speaking = True
                    session.audio_buffer.extend(data)
                elif session.user_is_speaking:
                    session.silence_bytes += len(data)
                    session.audio_buffer.extend(data)

                should_trigger = (
                    session.user_is_speaking
                    and (
                        session.silence_bytes >= silence_bytes_trigger
                        or len(session.audio_buffer) >= max_speech_bytes
                    )
                )

                if should_trigger:
                    if session.speech_bytes >= min_speech_bytes:
                        speech_pcm = bytes(session.audio_buffer)
                        session.audio_buffer.clear()
                        session.user_is_speaking = False
                        session.speech_bytes = 0
                        session.silence_bytes = 0

                        speaker = session.segment_speaker or session.active_speaker or "Participant"
                        session.segment_speaker = None
                        asyncio.create_task(process_turn_fn(session, speech_pcm, speaker))
                    else:
                        session.audio_buffer.clear()
                        session.user_is_speaking = False
                        session.speech_bytes = 0
                        session.silence_bytes = 0

    except WebSocketDisconnect:
        pass
    except Exception as e:  # log every failure; Meeting BaaS reconnects the socket
        logger.error(f"❌ Exception in inbound audio stream: {type(e).__name__}: {e}")
    finally:
        if session.in_websocket is websocket:
            session.in_websocket = None
        print(f"🔌 [TERMINAL] Audio INPUT socket closed (session {session.session_id}); "
              f"active input socket remaining: {session.in_websocket is not None}", flush=True)

async def wait_until_admitted(session: VoiceSession, timeout_secs: Optional[float] = None) -> bool:
    """Polls provider status until bot enters the call. Returns False on failure/end."""
    if not session.bot_id:
        return True
    from app.services.meeting_baas import meeting_baas_service

    limit = timeout_secs if timeout_secs is not None else settings.admission_wait_timeout_secs
    deadline = time.time() + limit
    announced = False
    while time.time() < deadline:
        try:
            status = (await asyncio.to_thread(meeting_baas_service.get_status, session.bot_id)).status
        except Exception as e:  # incl. MeetingServiceError: keep polling, never kill the greeting
            logger.debug(f"Status check failed while waiting for admission: {e}")
            status = None

        if status == "in_call":
            return True
        if status in ("ended", "failed"):
            return False
        if not announced:
            print(f"⏳ [TERMINAL] Bot not admitted yet (status: {status}) - greeting will play once it's in the call.", flush=True)
            announced = True
        await asyncio.sleep(1.5)
    logger.warning("⚠️ Timed out waiting for admission status; greeting anyway.")
    return True

async def greet_when_admitted(
    session: VoiceSession,
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
) -> None:
    """Plays entry greeting once bot is admitted to call."""
    if session.greeted or not session.entry_message:
        return
    if not await wait_until_admitted(session):
        return
    await asyncio.sleep(1.0)
    if session.greeted or not session.out_websocket:
        return
    session.greeted = True
    await speak_text(session, session.entry_message, groq_client, http_client)

async def handle_audio_out(
    session: VoiceSession,
    websocket: WebSocket,
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
) -> None:
    """
    Handles Meeting BaaS input_url connection. When connected, greets the call,
    then remains open to receive synthesized bot speech.
    """
    session.out_websocket = websocket
    logger.info(f"📢 Outbound audio stream connected for session {session.session_id}")
    print("\n📢 [TERMINAL] Google Meet Audio Output Connected! Bot microphone active.", flush=True)

    greet_task = asyncio.create_task(greet_when_admitted(session, groq_client, http_client))

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"❌ Exception in outbound audio stream: {type(e).__name__}: {e}")
    finally:
        if session.out_websocket is websocket:
            session.out_websocket = None
        print(f"🔌 [TERMINAL] Audio OUTPUT socket closed (session {session.session_id}); "
              f"active output socket remaining: {session.out_websocket is not None}", flush=True)
        if not greet_task.done() and session.out_websocket is None:
            greet_task.cancel()
