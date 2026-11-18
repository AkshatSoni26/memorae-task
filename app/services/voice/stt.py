import json
from typing import Optional
import httpx
from groq import AsyncGroq, GroqError
from app.core.config import settings
from app.core.logger import logger
from app.services.voice.addressing import other_ai_display_names
from app.services.voice.session import VoiceSession

def get_sibling_bot_names(
    session: VoiceSession,
    all_sessions: list[VoiceSession],
    known_ai_names: set[str]
) -> list[str]:
    """Display names of OTHER bots run by this server (AI participants)."""
    names = {s.bot_name.strip() for s in all_sessions if s is not session and s.bot_name}
    names |= {n for n in known_ai_names}
    own = session.bot_name.lower().strip()
    return sorted(n for n in names if n.lower() != own)

def get_stt_keyterms(session: VoiceSession, sibling_bots: list[str]) -> list[str]:
    """Names Deepgram should listen for, all taken from the live meeting:
    this bot, everyone Meet reports in the call, our other bots, and OTHER_AI_PARTICIPANTS."""
    names: list[str] = [session.bot_name.strip()]
    names += sorted(session.participant_names)
    names += sibling_bots
    names += other_ai_display_names()
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.lower().strip()
        if n and key not in seen:
            seen.add(key)
            out.append(n.strip())
    return out[:settings.stt_max_keyterms]

def get_stt_prompt(session: VoiceSession, sibling_bots: list[str]) -> str:
    """Whisper spelling hint: names in the call so Whisper doesn't misspell names."""
    names: list[str] = [session.bot_name.strip()]
    names += sorted(session.participant_names)
    names += sibling_bots
    names += other_ai_display_names()
    seen: set[str] = set()
    unique = [n for n in names if n and not (n.lower() in seen or seen.add(n.lower()))]
    participants = ", ".join(unique[:settings.stt_max_prompt_participants])
    return f"{settings.stt_prompt_prefix}{participants}."[:settings.stt_max_prompt_chars]

async def transcribe_deepgram(
    http_client: httpx.AsyncClient,
    session: VoiceSession,
    wav_payload: bytes,
    sibling_bots: list[str]
) -> str:
    """Deepgram Nova-3 pre-recorded transcription of one speech segment."""
    params: list[tuple[str, str]] = [
        ("model", settings.deepgram_stt_model),
        ("language", settings.stt_language),
        ("smart_format", "true" if settings.deepgram_smart_format else "false"),
        ("punctuate", "true" if settings.deepgram_punctuate else "false"),
    ]
    params += [("keyterm", k) for k in get_stt_keyterms(session, sibling_bots)]
    resp = await http_client.post(
        settings.deepgram_listen_url,
        params=params,
        headers={"Authorization": f"Token {settings.deepgram_api_key}", "Content-Type": "audio/wav"},
        content=wav_payload,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Deepgram STT error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        return (data["results"]["channels"][0]["alternatives"][0]["transcript"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Deepgram STT unexpected response: {str(data)[:200]}")

async def transcribe_groq(
    groq_client: AsyncGroq,
    session: VoiceSession,
    wav_payload: bytes,
    sibling_bots: list[str]
) -> str:
    """Groq Whisper speech-to-text transcription."""
    stt_result = await groq_client.audio.transcriptions.create(
        file=("speech.wav", wav_payload, "audio/wav"),
        model=settings.groq_stt_model,
        language=settings.stt_language,
        temperature=settings.groq_stt_temperature,
        prompt=get_stt_prompt(session, sibling_bots),
    )
    return stt_result.text.strip()

async def transcribe_speech(
    session: VoiceSession,
    wav_payload: bytes,
    sibling_bots: list[str],
    groq_client: AsyncGroq,
    http_client: httpx.AsyncClient,
) -> Optional[str]:
    """Dispatches STT transcription to configured engine and returns transcribed text."""
    stt_engine = settings.speech_provider
    try:
        if stt_engine == "deepgram":
            return await transcribe_deepgram(http_client, session, wav_payload, sibling_bots)
        return await transcribe_groq(groq_client, session, wav_payload, sibling_bots)
    except (httpx.HTTPError, GroqError, json.JSONDecodeError, KeyError, IndexError, TypeError, RuntimeError, OSError) as stt_err:
        logger.error(f"❌ {stt_engine} STT error: {stt_err}")
        print(f"❌ [STT ERROR] {stt_engine}: {stt_err}", flush=True)
        return None
