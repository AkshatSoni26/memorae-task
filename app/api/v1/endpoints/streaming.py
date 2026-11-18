import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.core.logger import logger
from app.services.groq_voice_pipeline import groq_voice_pipeline

router = APIRouter(prefix="/streaming", tags=["Streaming WebSockets"])

@router.websocket("/ws/audio-in/{session_id}")
async def ws_audio_in(websocket: WebSocket, session_id: str):
    """
    Receives raw meeting audio (24kHz PCM) from Meeting BaaS output_url.
    """
    await websocket.accept()
    session = groq_voice_pipeline.get_session(session_id)
    if not session:
        logger.warning(f"⚠️ Creating ad-hoc voice session for audio-in: {session_id}")
        session = groq_voice_pipeline.create_session(session_id)

    await groq_voice_pipeline.handle_audio_in(session, websocket)

@router.websocket("/ws/audio-out/{session_id}")
async def ws_audio_out(websocket: WebSocket, session_id: str):
    """
    Streams synthesized speech (24kHz PCM) back to Meeting BaaS input_url to speak in Google Meet.
    """
    await websocket.accept()
    session = groq_voice_pipeline.get_session(session_id)
    if not session:
        logger.warning(f"⚠️ Creating ad-hoc voice session for audio-out: {session_id}")
        session = groq_voice_pipeline.create_session(session_id)

    await groq_voice_pipeline.handle_audio_out(session, websocket)

@router.websocket("/ws/audio/{session_id}")
async def ws_audio_bidirectional(websocket: WebSocket, session_id: str):
    """
    Unified bidirectional WebSocket for clients/proxies that use a single socket.
    """
    await websocket.accept()
    session = groq_voice_pipeline.get_session(session_id)
    if not session:
        session = groq_voice_pipeline.create_session(session_id)

    session.out_websocket = websocket
    asyncio.create_task(groq_voice_pipeline._greet_when_admitted(session))
    await groq_voice_pipeline.handle_audio_in(session, websocket)
    if session.out_websocket is websocket:
        session.out_websocket = None
