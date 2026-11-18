"""
Background meeting notes: keeps `session.memory.notes` up to date without ever
blocking a reply.

Every few new transcript lines (or after a quiet spell), one call to a small, fast Groq
model merges the new lines into the current notes. That model has its own rate limit,
separate from the reply model's. Notes are saved to .runtime/ so a server restart in the
middle of a call keeps them. If an update fails, the lines simply stay in the word-for-word
window and are folded in on the next update.
"""
import asyncio
import json
import time
from typing import Optional

from groq import AsyncGroq

from app.core.config import settings
from app.core.logger import logger
from app.services.voice.constants import SESSIONS_FILE

NOTES_DIR = SESSIONS_FILE.parent / "notes"

NOTES_SYSTEM_PROMPT = (
    "You maintain compact running notes of a live meeting for an AI participant named {bot}. "
    "Merge the NEW transcript lines into the CURRENT notes and output the full updated notes. "
    "Use plain text with exactly these sections:\n"
    "PEOPLE: one line per person: name - role, company, key details\n"
    "FACTS: only specific, memorable facts: numbers, dates, names, preferences, and anything "
    "someone asked to be remembered, each attributed to the person who said it. Routine "
    "discussion does NOT go here.\n"
    "TODO: decisions and action items with owner and deadline\n"
    "SUMMARY: 2-5 sentences covering everything discussed so far, oldest topics included. "
    "Routine discussion goes here, condensed by topic.\n"
    "HARD LIMIT: the whole output must stay under {words} words, however long the meeting gets. "
    "As it grows, merge related items and compress older topics in the summary into fewer "
    "words, but never drop people, numbers, dates, action items or things someone asked to "
    "be remembered.\n"
    "Rules: keep concrete details from the current notes unless the new lines correct them; "
    "refer to people by name, never by pronouns; drop greetings and small talk; lines from "
    "'{bot} (you)' are {bot}'s own answers; no markdown."
)

_client: Optional[AsyncGroq] = None


def _get_client() -> AsyncGroq:
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=settings.groq_api_key)
    return _client


class NotesUpdater:
    """Per-session background updater for `session.memory.notes`."""

    def __init__(self, session):
        self.session = session
        self._task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None

    @property
    def _notes_file(self):
        return NOTES_DIR / f"{self.session.session_id}.json"

    # ---- called by MeetingMemory on every new line ----------------------------------
    def notify(self) -> None:
        if not settings.memory_notes_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (plain sync use): notes are an optional extra
        if self._task and not self._task.done():
            return  # the running update loops until nothing is left
        pending = self.session.memory.pending_count
        if pending >= settings.memory_notes_batch_lines:
            self._task = loop.create_task(self._run())
        elif pending > 0 and (self._idle_task is None or self._idle_task.done()):
            self._idle_task = loop.create_task(self._after_quiet())

    async def _after_quiet(self) -> None:
        """Folds in a small leftover batch once the room has been quiet for a while."""
        try:
            await asyncio.sleep(settings.memory_notes_idle_secs)
            if self.session.memory.pending_count > 0 and (self._task is None or self._task.done()):
                self._task = asyncio.get_running_loop().create_task(self._run())
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        memory = self.session.memory
        bot = self.session.bot_name
        try:
            while memory.pending_count > 0:
                start, end = memory.noted_upto, memory.total_appended
                lines = memory.entries_between(start, end)
                if not lines:
                    memory.apply_notes(memory.notes, end)
                    continue
                t0 = time.time()
                new_text = "\n".join(e.format_line() for e in lines)
                notes = await self._call_model(bot, memory.notes, new_text)
                if not notes:
                    logger.warning("⚠️ [MEETING NOTES] Empty notes from model; keeping the old ones.")
                    return
                memory.apply_notes(notes, end)
                self._save()
                print(
                    f"📝 [MEETING NOTES] Folded {len(lines)} lines in {time.time() - t0:.2f}s "
                    f"| notes {len(notes)} chars | {memory.pending_count} lines pending",
                    flush=True,
                )
                if memory.pending_count < settings.memory_notes_batch_lines:
                    # leave a small tail for the next batch, or fold it in after a quiet spell
                    if memory.pending_count > 0 and (self._idle_task is None or self._idle_task.done()):
                        self._idle_task = asyncio.get_running_loop().create_task(self._after_quiet())
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:  # lines stay in the word-for-word window; retried next time
            logger.warning(f"⚠️ [MEETING NOTES] Update failed ({type(e).__name__}): {e}")

    async def _call_model(self, bot: str, current_notes: str, new_text: str) -> str:
        """One notes-merge call; on a rate limit, waits briefly and retries (twice at most)."""
        from groq import RateLimitError

        for attempt in range(3):
            try:
                resp = await _get_client().chat.completions.create(
                    model=settings.memory_notes_model,
                    messages=[
                        {"role": "system", "content": NOTES_SYSTEM_PROMPT.format(
                            bot=bot, words=settings.memory_notes_max_words)},
                        {"role": "user", "content": (
                            f"CURRENT NOTES:\n{current_notes or '(none yet)'}\n\nNEW LINES:\n{new_text}"
                        )},
                    ],
                    max_tokens=settings.memory_notes_max_tokens,
                    temperature=0.2,
                    reasoning_effort="low",
                )
                return (resp.choices[0].message.content or "").strip()
            except RateLimitError:
                if attempt == 2:
                    raise
                await asyncio.sleep(5.0 * (attempt + 1))
        return ""

    # ---- restart safety ---------------------------------------------------------------
    def _save(self) -> None:
        try:
            NOTES_DIR.mkdir(parents=True, exist_ok=True)
            self._notes_file.write_text(
                json.dumps({"notes": self.session.memory.notes, "saved_at": time.time()}),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"⚠️ [MEETING NOTES] Could not save notes: {e}")

    def restore(self) -> bool:
        """Loads notes saved before a server restart. Returns True if any were found."""
        try:
            if self._notes_file.exists():
                notes = json.loads(self._notes_file.read_text(encoding="utf-8")).get("notes", "")
                if notes:
                    self.session.memory.apply_notes(notes, 0)
                    return True
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"⚠️ [MEETING NOTES] Could not load saved notes: {e}")
        return False
