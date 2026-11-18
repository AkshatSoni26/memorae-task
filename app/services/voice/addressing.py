import difflib
import re
import time
from typing import Optional
from app.core.config import settings
from app.services.voice.constants import (
    AI_REPLY_STREAK_MAX,
    BACKCHANNEL_PHRASES,
    GROUP_ADDRESS_RE,
    LEADING_FILLERS_RE,
    QUESTION_STARTERS,
)
from app.services.voice.session import VoiceSession

def clean_for_speech(text: str) -> str:
    """
    Strips reasoning tags (<think>...</think>) and markdown artifacts so
    TTS always speaks clean, natural prose.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"[*_`#]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def looks_like_follow_up(text_lower: str) -> bool:
    """Heuristic: is this a question/request/remark aimed at the bot during an ongoing exchange?"""
    stripped = text_lower.strip().rstrip(".!")
    if not stripped or stripped in BACKCHANNEL_PHRASES:
        return False
    if stripped.endswith("?"):
        return True
    if re.search(r"\b(?:you|your|yours|yourself|you're|you've|you'll)\b", stripped):
        return True
    stripped = LEADING_FILLERS_RE.sub("", stripped)
    words = stripped.split()
    return bool(words) and words[0].strip(",") in QUESTION_STARTERS

def name_variants(bot_name_lower: str) -> list[str]:
    """Extra spellings of the bot's name, configured via BOT_NAME_ALIASES in .env."""
    variants = [a.strip().lower() for a in settings.bot_name_aliases.split(",") if a.strip()]
    return [v for v in variants if v and v != bot_name_lower]

def other_ai_display_names() -> list[str]:
    """Other AI agents expected in the call, from OTHER_AI_PARTICIPANTS in .env (original casing)."""
    return [k.strip() for k in settings.other_ai_participants.split(",") if k.strip()]

def other_ai_names() -> list[str]:
    """Lowercased OTHER_AI_PARTICIPANTS, for matching."""
    return [k.lower() for k in other_ai_display_names()]

def fuzzy_vocative(words: list[str], bot_name_lower: str) -> Optional[str]:
    """
    Whisper near-misses of the name ('javish', 'jaavish') in the vocative spots only:
    first word, last word, or right after a greeting. Returns the misheard word, or None.
    """
    if len(bot_name_lower) < 5 or not words:
        return None
    candidates = {words[0], words[-1]}
    for i, w in enumerate(words[:-1]):
        if w in ("hey", "hi", "hello", "ok", "okay", "yo", "thanks"):
            candidates.add(words[i + 1])
    for w in candidates:
        if (
            w != bot_name_lower
            and w[:1] == bot_name_lower[:1]
            and abs(len(w) - len(bot_name_lower)) <= 2
            and difflib.SequenceMatcher(None, w, bot_name_lower).ratio() >= 0.75
        ):
            return w
    return None

def is_ai_participant(name: str, bot_name: str, sibling_bot_names: list[str]) -> bool:
    """Identifies other AI agents / notetakers in the call (by display name)."""
    lowered = name.lower()
    if lowered == bot_name.lower().strip():
        return False
    if lowered in (n.lower() for n in sibling_bot_names):
        return True
    if any(k in lowered for k in other_ai_names()):
        return True
    return bool(re.search(
        r"\b(?:bot|ai|notetaker|note taker|assistant|recorder|fireflies|otter|fathom|tldv|read\.ai)\b",
        lowered,
    ))

def evaluate_addressing(
    session: VoiceSession,
    user_text: str,
    speaker: str = "Participant",
    sibling_bot_names: Optional[list[str]] = None,
    follow_up_window_secs: float = 30.0,
) -> tuple[bool, str]:
    """
    Multi-bot safe addressing gate:
    - Name said (incl. common Whisper misspellings) -> addressed.
    - Blocks 'Memorae' / 'Memo ray' / 'Boardy' collisions.
    - Conversation mode: while the SAME person who last talked with the bot keeps talking
      (no gap longer than follow_up_window_secs), un-named questions / "you" remarks count too.
    - Ignores all other speech and saves it as passive context.
    """
    text_lower = user_text.lower().strip()
    bot_name_lower = session.bot_name.lower().strip()
    siblings = sibling_bot_names or []

    if not bot_name_lower:
        return False, "no_bot_name"

    is_ai = speaker != "Participant" and is_ai_participant(speaker, session.bot_name, siblings)
    if not is_ai and speaker != "Participant":
        session.ai_reply_streak = 0

    now = time.time()
    in_conversation = (
        speaker != "Participant"
        and session.last_interlocutor == speaker
        and now - session.conversation_active_at <= follow_up_window_secs
    )
    if in_conversation:
        session.conversation_active_at = now

    sanitized_text = text_lower

    variants = name_variants(bot_name_lower)
    if variants:
        variant_re = r"\b(?:" + "|".join(re.escape(v) for v in variants) + r")\b"
        sanitized_text = re.sub(variant_re, bot_name_lower, sanitized_text)

    name_re = re.escape(bot_name_lower)
    if not re.search(rf"\b{name_re}\b", sanitized_text):
        misheard = fuzzy_vocative(re.sub(r"[^a-z0-9' ]", " ", sanitized_text).split(), bot_name_lower)
        if misheard:
            sanitized_text = re.sub(rf"\b{re.escape(misheard)}\b", bot_name_lower, sanitized_text)

    if re.search(rf"\b{name_re}\b", sanitized_text):
        words_only = re.sub(r"[^a-z0-9' ]", " ", sanitized_text).split()
        is_direct = bool(
            (words_only and (words_only[0] == bot_name_lower or words_only[-1] == bot_name_lower))
            or re.search(rf"\b(?:hey|hi|hello|ok|okay|yo|thanks|thank you)\s*,?\s*{name_re}\b", sanitized_text)
            or re.search(rf"(?:^|[,.!?;:]\s*){name_re}\s*(?:[,.!?;:]|$)", sanitized_text)
            or re.search(rf"[,]\s*{name_re}\b", sanitized_text)
        )
        if is_ai:
            if not is_direct:
                return False, "ai_participant_mention"
            if session.ai_reply_streak >= AI_REPLY_STREAK_MAX:
                return False, "ai_loop_guard"
            return True, "ai_direct_address"
        if is_direct:
            return True, "direct_address"
        return True, "name_mention"

    if is_ai:
        return False, "ai_participant_speaking"

    other_bots = [name for name in other_ai_names() if name != bot_name_lower]
    other_bots += [n.lower() for n in siblings]
    if other_bots:
        other_pattern = r"\b(?:" + "|".join(re.escape(n) for n in other_bots) + r")\b"
        if re.search(other_pattern, sanitized_text):
            return False, "other_bot_addressed"

    if re.search(r"\byourselves\b", sanitized_text) or (
        GROUP_ADDRESS_RE.search(sanitized_text) and looks_like_follow_up(sanitized_text)
    ):
        return True, "group_address"

    if in_conversation:
        speaker_first = speaker.split()[0].lower() if speaker.split() else ""
        for other in session.participant_names:
            first = other.split()[0].lower() if other.split() else ""
            if len(first) >= 3 and first != speaker_first and re.search(rf"\b{re.escape(first)}\b", sanitized_text):
                return False, f"addressed_to_{first}"
        if looks_like_follow_up(sanitized_text):
            return True, "follow_up"

    return False, "not_explicitly_addressed"
