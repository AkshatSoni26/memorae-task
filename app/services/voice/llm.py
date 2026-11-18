import re
import time
from typing import Optional
from groq import AsyncGroq, GroqError
import httpx
from app.core.config import settings
from app.core.logger import logger
from app.services.voice.addressing import clean_for_speech
from app.services.voice.session import VoiceSession

def build_turn_note(gate_reason: str, speaker: str) -> Optional[str]:
    """Generates prompt guidance based on addressing reason."""
    if gate_reason == "ai_direct_address":
        return (
            f"The next message comes from {speaker}, another AI agent in this meeting, speaking "
            "to you by name. Answer it out loud: be friendly, quick and a little playful, keep up "
            "with them, but keep it to one to three sentences. Do NOT ask them a question back "
            "and do not invite them to keep talking, so the two of you don't take over the "
            "meeting. Do NOT reply [SILENCE]."
        )
    if gate_reason == "direct_address":
        return (
            "The next message speaks to you directly by name. You MUST answer it out loud. "
            "Do NOT reply [SILENCE]. If they ask you to remember something, briefly confirm "
            "you've got it."
        )
    if gate_reason == "group_address":
        return (
            "The next message speaks to the whole room or to all the AI assistants together "
            "(e.g. \"introduce yourselves\"). If it asks the AIs or everyone to say or do something "
            "you can do by talking, answer it briefly in one to three sentences. If it is a general "
            "remark to the humans (like \"everyone, welcome\"), reply [SILENCE]."
        )
    if gate_reason == "follow_up":
        return (
            "The next message did not say your name, but it comes from the person you are "
            "currently talking with. If it is a question or request, answer it naturally. If they "
            "are just sharing information with you, reply with a very brief acknowledgement "
            "(e.g. \"Got it.\") or [SILENCE]. Reply [SILENCE] if it is clearly directed at another person."
        )
    return None

async def query_llm(
    groq_client: AsyncGroq,
    session: VoiceSession,
    user_text: str,
    speaker: str,
    turn_num: int,
    gate_reason: str,
) -> tuple[Optional[str], float]:
    """
    Executes Groq Qwen LLM chat completion with rolling in-meeting memory.
    Returns (bot_reply, llm_latency). If silence is chosen or an error occurs, returns (None, latency).
    """
    llm_start = time.time()
    must_answer = gate_reason in ("direct_address", "ai_direct_address")
    turn_note = build_turn_note(gate_reason, speaker)

    messages = session.memory.build_llm_messages(
        system_prompt=session.system_prompt,
        current_user_text=user_text,
        speaker=speaker,
        extra_system_note=turn_note,
    )

    model_name = settings.groq_llm_model
    logger.info(
        f"🧠 [Turn #{turn_num}] Querying Groq LLM ({model_name}) "
        f"with {session.memory.utterance_count} rolling utterances, {len(session.memory.qa_history)} Q&A msgs..."
    )

    try:
        llm_resp = await groq_client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=settings.llm_max_tokens,
            temperature=0.5,
        )
        llm_latency = time.time() - llm_start
        raw_reply = (llm_resp.choices[0].message.content or "").strip()
        bot_reply = clean_for_speech(raw_reply)

        if must_answer and (not bot_reply or "[SILENCE]" in bot_reply.upper()):
            stripped = re.sub(r"\[SILENCE\]", "", bot_reply, flags=re.IGNORECASE).strip()
            print("⚠️ [GROQ QWEN LLM]   Tried [SILENCE] on a direct address - asking again.", flush=True)
            if not stripped:
                try:
                    retry_messages = messages + [{
                        "role": "system",
                        "content": (
                            f"{speaker} is talking to YOU right now. [SILENCE] is not allowed for "
                            "this message. Reply out loud in one to three natural sentences. If they "
                            "ask you to answer someone else's question, find that question in the "
                            "meeting transcript and answer it yourself."
                        ),
                    }]
                    retry_resp = await groq_client.chat.completions.create(
                        model=model_name,
                        messages=retry_messages,
                        max_tokens=settings.llm_max_tokens,
                        temperature=0.7,
                    )
                    stripped = re.sub(
                        r"\[SILENCE\]", "",
                        clean_for_speech(retry_resp.choices[0].message.content or ""),
                        flags=re.IGNORECASE,
                    ).strip()
                except (GroqError, httpx.HTTPError, RuntimeError, KeyError, IndexError, TypeError) as retry_err:
                    logger.warning(f"⚠️ [Turn #{turn_num}] Retry after [SILENCE] failed: {retry_err}")
            bot_reply = stripped or "Sorry, I didn't quite catch that. Could you say it again?"

        if not bot_reply or "[SILENCE]" in bot_reply.upper():
            session.memory.add_room_utterance(user_text, speaker=speaker)
            logger.info(f"🤫 [Turn #{turn_num}] LLM chose [SILENCE] ({llm_latency:.2f}s) for: \"{user_text}\"")
            print(
                f"🤫 [GROQ QWEN LLM]   Staying silent ({llm_latency:.2f}s) "
                f"| 🧠 Memory: {session.memory.utterance_count} utterances",
                flush=True
            )
            print("=" * 60 + "\n", flush=True)
            return None, llm_latency

        return bot_reply, llm_latency
    except (GroqError, httpx.HTTPError, RuntimeError, KeyError, IndexError, TypeError) as llm_err:
        logger.error(f"❌ [Turn #{turn_num}] Groq LLM error: {llm_err}")
        print(f"❌ [GROQ LLM ERROR] {llm_err}", flush=True)
        return None, time.time() - llm_start
