import asyncio
from app.core.config import settings
from app.core.logger import logger
from app.services.session_store import session_store
from app.services.meeting_baas import meeting_baas_service
from app.services.groq_voice_pipeline import groq_voice_pipeline

# Background monitor task
_monitor_task = None

async def poll_active_bots_usage():
    """
    Background worker that continuously tracks active bot metrics (both Groq pipeline
    and Speaking Bots) every 2.5 seconds and logs live stats to the terminal.
    """
    last_seen_state = {}

    while True:
        try:
            latest_session = session_store.get_latest_bot()
            if latest_session and latest_session.get("status") in ["joining", "waiting_for_admission", "in_call"]:
                bot_id = latest_session.get("bot_id")
                
                # 1. Check Groq Voice Pipeline session
                groq_sess = groq_voice_pipeline.get_session(bot_id)
                if groq_sess:
                    turns = groq_sess.turns_count
                    prev = last_seen_state.get(bot_id, {})
                    if turns != prev.get("turns"):
                        print("\n" + "=" * 55)
                        print(f"⚡ [GROQ PIPELINE METRICS] Bot: {bot_id} ('{groq_sess.bot_name}')")
                        print(f"🧠 Conversational Turns:    {turns}")
                        print(f"🗂️ Rolling Meeting Memory:  {groq_sess.memory.utterance_count} utterances")
                        print(f"💬 Conversation History:   {len(groq_sess.conversation_history)} messages")
                        print(f"🔌 Audio In Connected:     {groq_sess.in_websocket is not None}")
                        print(f"📢 Audio Out Connected:    {groq_sess.out_websocket is not None}")
                        print("=" * 55 + "\n")
                        last_seen_state[bot_id] = {"turns": turns}
                else:
                    # 2. Speaking Bots usage API
                    usage = meeting_baas_service.get_bot_usage(bot_id)
                    if usage:
                        stt_sec = usage.get("stt_audio_seconds", 0)
                        llm_turns = usage.get("llm_turns", 0)
                        tts_req = usage.get("tts_requests", 0)
                        prev = last_seen_state.get(bot_id, {})

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
