import asyncio
import io
import re
import time
import wave
from typing import AsyncGenerator, List
from fastapi import WebSocketDisconnect
from groq import AsyncGroq, GroqError
import httpx
from app.core.config import settings
from app.core.logger import logger
from app.services.voice.constants import (
    BARGE_IN_SECS,
    CHUNK_SIZE_BYTES,
    FLOOR_WAIT_MAX_SECS,
    SPEAKING_GRACE_SECS,
)
from app.services.voice.session import VoiceSession

def split_sentences(text: str) -> List[str]:
    """Splits a reply into sentences for pipelined TTS; merges tiny fragments for natural prosody."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    merged: List[str] = []
    for part in parts:
        if merged and len(merged[-1]) < 25:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged or [text]

async def stream_deepgram(http_client: httpx.AsyncClient, text: str) -> AsyncGenerator[bytes, None]:
    """
    Deepgram Flux voice streamed as raw linear16 24kHz mono PCM.
    Yields audio chunks as they arrive for immediate playback.
    """
    params = {
        "model": settings.deepgram_tts_model,
        "encoding": settings.deepgram_encoding,
        "sample_rate": str(settings.audio_sample_rate),
        "container": settings.deepgram_container,
    }
    async with http_client.stream(
        "POST", settings.deepgram_speak_url, params=params,
        headers={"Authorization": f"Token {settings.deepgram_api_key}"},
        json={"text": text},
    ) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "replace")[:200]
            raise RuntimeError(f"Deepgram TTS error {resp.status_code}: {body}")
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk

async def synthesize_groq(groq_client: AsyncGroq, text: str) -> bytes:
    """Whole-sentence PCM synthesis using Groq Orpheus TTS."""
    tts_resp = await groq_client.audio.speech.create(
        model=settings.groq_tts_model,
        voice=settings.groq_tts_voice,
        input=text,
        response_format=settings.groq_tts_response_format,
    )
    wav_bytes = await tts_resp.read()
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes())

async def synthesize_pcm(groq_client: AsyncGroq, http_client: httpx.AsyncClient, text: str) -> bytes:
    """Synthesizes PCM from the configured provider without fallback."""
    if settings.speech_provider == "deepgram":
        pcm = b"".join([c async for c in stream_deepgram(http_client, text)])
        if not pcm:
            raise RuntimeError("Deepgram TTS returned no audio")
        return pcm
    return await synthesize_groq(groq_client, text)

def warm_up_tts(http_client: httpx.AsyncClient) -> None:
    """Pre-warms Deepgram TLS connection to avoid cold-start delay on first turn."""
    if settings.speech_provider != "deepgram":
        return

    async def warm() -> None:
        try:
            async for _ in stream_deepgram(http_client, "Hi."):
                pass
            print("🔈 [TTS] Deepgram text-to-speech warmed up", flush=True)
        except (httpx.HTTPError, RuntimeError, OSError) as warm_err:
            logger.warning(f"⚠️ TTS warm-up (deepgram) failed: {warm_err}")

    try:
        asyncio.get_running_loop().create_task(warm())
    except RuntimeError:
        pass

def expire_speakers(session: VoiceSession, now: float) -> None:
    """Drops speakers whose indicator has been off longer than the grace period."""
    for name in list(session.speaking_since):
        last_seen = session.speaking_last_seen.get(name, 0.0)
        if now - last_seen > SPEAKING_GRACE_SECS:
            del session.speaking_since[name]
            session.speaking_last_seen.pop(name, None)

def others_speaking(session: VoiceSession, started_after: float = 0.0, min_secs: float = 0.0) -> list[str]:
    """Participants currently speaking excluding this bot (filters noisy open mics > 15s)."""
    now = time.time()
    expire_speakers(session, now)
    talking = []
    for name, since in session.speaking_since.items():
        heard_for = session.speaking_last_seen.get(name, since) - since
        if since >= started_after and heard_for >= min_secs and now - since <= 15.0:
            talking.append(name)
    return talking

async def wait_for_floor(session: VoiceSession, max_wait_secs: float = FLOOR_WAIT_MAX_SECS) -> None:
    """Polite turn-taking: waits if someone else just started talking."""
    waited = 0.0
    announced = False
    while waited < max_wait_secs:
        talking = others_speaking(session)
        if not talking and not session.user_is_speaking:
            return
        if not announced:
            who = ", ".join(talking) or "someone"
            print(f"⏸️ [FLOOR] {who} is talking - holding reply (max {max_wait_secs:.0f}s)...", flush=True)
            announced = True
        await asyncio.sleep(0.1)
        waited += 0.1

async def produce_audio(
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
    text: str,
    queue: asyncio.Queue
) -> None:
    """Fills queue with PCM bytes for text, then signals completion with None."""
    try:
        if settings.speech_provider == "deepgram":
            async for chunk in stream_deepgram(http_client, text):
                await queue.put(chunk)
            return

        sentences = split_sentences(text)
        slots = asyncio.Semaphore(3)

        async def synth(sentence_text: str) -> bytes:
            async with slots:
                return await synthesize_groq(groq_client, sentence_text)

        tasks = [asyncio.create_task(synth(s)) for s in sentences]
        try:
            for task in tasks:
                await queue.put(await task)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as prod_err:  # any TTS failure is handed to the consumer and logged there
        await queue.put(prod_err)
    finally:
        await queue.put(None)

async def speak_text_locked(
    session: VoiceSession,
    text: str,
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
    chunk_size_bytes: int = CHUNK_SIZE_BYTES,
) -> float:
    """Streams audio into the meeting socket with barge-in support. Returns TTS latency in seconds."""
    tts_label = f"Deepgram {settings.deepgram_tts_model}" if settings.speech_provider == "deepgram" else "Groq Orpheus"
    logger.info(f"🔊 [TTS: {tts_label}] Synthesizing: \"{text}\"")
    tts_start = time.time()
    tts_latency = 0.0
    queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(produce_audio(groq_client, http_client, text, queue))
    buffer = b""
    playback_started = 0.0
    finished = False

    try:
        while True:
            while not finished and len(buffer) < chunk_size_bytes:
                item = await queue.get()
                if item is None:
                    finished = True
                elif isinstance(item, BaseException):
                    raise item
                else:
                    buffer += item
            if len(buffer) < 2:
                break

            if not playback_started:
                tts_latency = time.time() - tts_start
                print(f"🔊 [TTS: {tts_label}] First audio ready in {tts_latency:.2f}s", flush=True)
                await wait_for_floor(session)
                session.is_speaking = True
                playback_started = time.time()
                print(f"▶️ [GOOGLE MEET] Speaking out loud to meeting: \"{text}\"", flush=True)

            ws = session.out_websocket
            if ws is None:
                print("❌ [GOOGLE MEET] Output socket lost mid-speech - meeting did not hear the rest.", flush=True)
                return tts_latency

            interrupters = others_speaking(session, started_after=playback_started, min_secs=BARGE_IN_SECS)
            if interrupters:
                print(f"🛑 [BARGE-IN] {', '.join(interrupters)} started talking - stopping playback.", flush=True)
                return tts_latency

            take = chunk_size_bytes if len(buffer) >= chunk_size_bytes else len(buffer) - (len(buffer) % 2)
            await asyncio.wait_for(ws.send_bytes(buffer[:take]), timeout=settings.ws_send_timeout_secs)
            buffer = buffer[take:]
            await asyncio.sleep(0.02)

        logger.info(f"⏹️ Finished audio playback for: \"{text}\"")
        print("⏹️ [GOOGLE MEET] Finished audio playback.", flush=True)
        return tts_latency
    except Exception as e:  # playback must never crash the turn (socket drops, TTS errors, ...)
        logger.error(f"❌ Error in TTS playback: {type(e).__name__}: {e}")
        print(f"❌ [TTS ERROR] {e}", flush=True)
        return tts_latency
    finally:
        if not producer.done():
            producer.cancel()
        session.is_speaking = False

async def speak_text(
    session: VoiceSession,
    text: str,
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
    chunk_size_bytes: int = CHUNK_SIZE_BYTES,
) -> float:
    """Public helper that acquires speak_lock and streams audio to outbound websocket."""
    if not session.out_websocket:
        print(f"❌ [GOOGLE MEET] Cannot speak - no output socket connected (session {session.session_id})", flush=True)
        return 0.0

    async with session.speak_lock:
        return await speak_text_locked(session, text, groq_client, http_client, chunk_size_bytes)
