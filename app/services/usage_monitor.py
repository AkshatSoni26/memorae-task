import asyncio
from app.core.config import settings
from app.core.logger import logger
from app.services.session_store import session_store
from app.services.meeting_baas import meeting_baas_service

# Background monitor task
_monitor_task = None

async def poll_active_bots_usage():
    """
    Background worker that continuously polls the live Speaking Bot usage metrics
    every 3 seconds and prints audio seconds, LLM turns, and speaker activity to the terminal.
    """
    last_seen_state = {}

    while True:
        try:
            latest_session = session_store.get_latest_bot()
            if latest_session and latest_session.get("status") in ["joining", "in_call", "in_call_recording"]:
                bot_id = latest_session.get("bot_id")
                usage = meeting_baas_service.get_bot_usage(bot_id)
                if usage:
                    stt_sec = usage.get("stt_audio_seconds", 0)
                    llm_turns = usage.get("llm_turns", 0)
                    tts_req = usage.get("tts_requests", 0)
                    prev = last_seen_state.get(bot_id, {})

                    # If audio seconds increased or turn happened, print clear diagnostic log
                    if stt_sec != prev.get("stt_sec") or llm_turns != prev.get("llm_turns") or tts_req != prev.get("tts_req"):
                        print("\n" + "-" * 55)
                        print(f"🎙️  [LIVE MEETING METRICS] Bot: {bot_id}")
                        print(f"👂 Audio Captured from You: {stt_sec:.1f}s")
                        print(f"🧠 AI Conversational Turns: {llm_turns}")
                        print(f"🗣️  Speech Audio Plays:      {tts_req}")
                        print("-" * 55 + "\n")

                        last_seen_state[bot_id] = {
                            "stt_sec": stt_sec,
                            "llm_turns": llm_turns,
                            "tts_req": tts_req
                        }
        except Exception:
            pass

        await asyncio.sleep(2.5)

def start_usage_monitor():
    global _monitor_task
    if _monitor_task is None:
        _monitor_task = asyncio.create_task(poll_active_bots_usage())
