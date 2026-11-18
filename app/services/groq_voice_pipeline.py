import asyncio
import io
import json
import os
import re
import struct
import time
import wave
from typing import Dict, List, Optional
from fastapi import WebSocket, WebSocketDisconnect
from groq import AsyncGroq
from app.core.config import settings
from app.core.logger import logger
from app.prompts import load_system_prompt
from app.services.meeting_memory import MeetingMemory

class VoiceSession:
    def __init__(self, session_id: str, bot_name: str, system_prompt: str, entry_message: str):
        self.session_id: str = session_id
        self.bot_name: str = bot_name
        self.system_prompt: str = system_prompt
        self.entry_message: str = entry_message
        
        # Audio & WebSocket channels
        self.in_websocket: Optional[WebSocket] = None
        self.out_websocket: Optional[WebSocket] = None
        
        # Rolling in-meeting memory (chronological transcript + Q&A history)
        self.memory: MeetingMemory = MeetingMemory(bot_name=bot_name)
        self.last_bot_spoke_at: float = 0.0
        
        # Audio state
        self.sample_rate: int = 24000
        self.sample_width: int = 2  # 16-bit
        self.channels: int = 1      # Mono
        
        # VAD & Buffer state
        self.audio_buffer: bytearray = bytearray()
        self.is_speaking: bool = False       # True when bot is currently talking
        self.user_is_speaking: bool = False  # True when user speech is in progress
        self.silence_bytes: int = 0
        self.speech_bytes: int = 0
        self.frames_received: int = 0
        self.lock: asyncio.Lock = asyncio.Lock()
        # Serializes reply generation + playback so two turns never talk over each other
        self.turn_lock: asyncio.Lock = asyncio.Lock()
        # Serializes writes to the outbound audio socket (greeting vs. replies)
        self.speak_lock: asyncio.Lock = asyncio.Lock()

        # Provider bot_id (set once dispatch returns) and one-time greeting flag
        self.bot_id: Optional[str] = None
        self.greeted: bool = False

        # Speaker attribution from Meeting BaaS speaker events
        self.active_speaker: Optional[str] = None   # last human Meet reported as speaking
        self.segment_speaker: Optional[str] = None  # speaker captured for the current speech segment

        # Conversation mode: who the bot is currently talking with, and when they last spoke
        self.last_interlocutor: Optional[str] = None
        self.conversation_active_at: float = 0.0

        # Multi-party awareness from Meet speaker indicators
        self.participant_names: set[str] = set()       # everyone seen in the call (except this bot)
        self.speaking_since: Dict[str, float] = {}     # who is speaking right now -> since when
        self.speaking_last_seen: Dict[str, float] = {} # last time their speaking indicator was on

        # Diagnostics
        self.turns_count: int = 0
        self.total_speech_secs: float = 0.0

    @property
    def conversation_history(self) -> List[Dict[str, str]]:
        return self.memory.qa_history

class GroqVoicePipelineManager:
    # Common Whisper transcriptions of sibling bots in multi-bot Google Meets
    OTHER_BOT_NAMES: tuple[str, ...] = (
        "memorae", "memori", "memorie", "memoree", "memoray", "memorai",  # Whisper spellings of Memorae
        "boardy", "bordy", "boardie",
    )
    BACKCHANNEL_PHRASES: frozenset[str] = frozenset({
        "ok", "okay", "k", "thanks", "thank you", "cool", "nice", "great",
        "got it", "yeah", "yes", "yep", "nope", "alright", "all right",
        "uh-huh", "mm-hmm", "hmm", "right", "sure", "awesome", "perfect",
    })
    # Phrases Whisper commonly invents from noise/silence; never treat them as real speech
    WHISPER_HALLUCINATIONS: frozenset[str] = frozenset({
        "you", "thank you", "thanks", "thank you so much", "thanks for watching",
        "thank you for watching", "bye", "bye bye", "uh", "um", "hmm", "so",
    })
    # Openers that make an un-named utterance look like a question/request to the bot
    QUESTION_STARTERS: frozenset[str] = frozenset({
        "what", "why", "how", "when", "where", "who", "whom", "whose", "which",
        "can", "could", "would", "should", "will", "is", "are", "was", "were",
        "do", "does", "did", "have", "has", "tell", "explain", "give", "say",
        "let", "repeat", "remind", "remember",
    })
    # Common Whisper mishearings of a bot name (whole words only)
    NAME_MISHEARINGS: dict[str, tuple[str, ...]] = {
        "memo": ("mamo", "mehmo", "meemo", "memmo", "mimo", "memoh", "mammo"),
        "jarvis": ("jervis", "jarvus", "jarves", "jarviss", "jarvice", "javis", "jaavis"),
    }

    def __init__(self):
        self.sessions: Dict[str, VoiceSession] = {}
        self.groq_client: Optional[AsyncGroq] = None
        
        # VAD Tuning Constants (at 24kHz mono 16-bit = 48,000 bytes/sec)
        self.chunk_size_bytes: int = 960            # 20ms playback chunk
        self.bytes_per_second: int = 48000          # 24000 Hz * 2 bytes
        self.silence_threshold_rms: float = 300.0   # Sensitive speech detection
        self.silence_bytes_trigger: int = 36000     # 0.75s silence pause prevents mid-sentence cutoffs
        self.min_speech_bytes: int = 14400          # 0.30s min speech to avoid pops
        self.max_speech_bytes: int = 960000         # 20.0s max buffer cap (10s was cutting long sentences)
        self.follow_up_window_secs: float = 30.0    # Conversation mode idle timeout (same speaker)

    def _get_client(self) -> AsyncGroq:
        if self.groq_client is None:
            api_key = settings.groq_api_key or os.getenv("GROQ_API_KEY", "")
            self.groq_client = AsyncGroq(api_key=api_key)
        return self.groq_client

    def create_session(
        self,
        session_id: str,
        bot_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        entry_message: Optional[str] = None
    ) -> VoiceSession:
        name = bot_name or settings.default_bot_name
        prompt = system_prompt or load_system_prompt(bot_name=name)
        greeting = entry_message or settings.default_entry_message
        
        session = VoiceSession(
            session_id=session_id,
            bot_name=name,
            system_prompt=prompt,
            entry_message=greeting
        )
        self.sessions[session_id] = session
        logger.info(f"🎙️ Created Groq voice pipeline session: {session_id} for bot '{name}'")
        return session

    def get_session(self, session_id: str) -> Optional[VoiceSession]:
        return self.sessions.get(session_id)

    def alias_session(self, alias_id: str, session_id: str) -> None:
        """Makes a session reachable by a second key (e.g. the provider bot_id)."""
        if session_id in self.sessions:
            self.sessions[alias_id] = self.sessions[session_id]
            self.sessions[session_id].bot_id = alias_id

    def remove_session(self, key: str) -> None:
        """Drops a session and every alias pointing at it."""
        session = self.sessions.get(key)
        if session is None:
            return
        for k in [k for k, v in self.sessions.items() if v is session]:
            del self.sessions[k]
        logger.info(f"🧹 Removed voice session {session.session_id}")

    LEADING_FILLERS_RE = re.compile(
        r"^(?:(?:and|so|but|okay|ok|alright|all right|then|now|yeah|yes|yep|well|um|uh|hmm|right)\b[,.\s]*)+"
    )

    def _looks_like_follow_up(self, text_lower: str) -> bool:
        """Heuristic: is this a question/request/remark aimed at the bot during an ongoing exchange?"""
        stripped = text_lower.strip().rstrip(".!")
        if not stripped or stripped in self.BACKCHANNEL_PHRASES:
            return False
        if stripped.endswith("?"):
            return True
        # Second person ("can you…", "I'm telling you…", "your name") during an exchange
        if re.search(r"\b(?:you|your|yours|yourself|you're|you've|you'll)\b", stripped):
            return True
        # Question/request opener, after skipping fillers like "yeah okay now so…"
        stripped = self.LEADING_FILLERS_RE.sub("", stripped)
        words = stripped.split()
        return bool(words) and words[0].strip(",") in self.QUESTION_STARTERS

    def _name_variants(self, bot_name_lower: str) -> list[str]:
        """Bot name plus common Whisper misspellings (built-in for 'memo', extendable via env)."""
        variants = list(self.NAME_MISHEARINGS.get(bot_name_lower, ()))
        variants += [a.strip().lower() for a in settings.bot_name_aliases.split(",") if a.strip()]
        return [v for v in variants if v and v != bot_name_lower]

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """
        Strips reasoning tags (<think>...</think>) and markdown artifacts so
        Orpheus TTS always speaks clean, natural prose.
        """
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"[*_`#]+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _evaluate_addressing(
        self, session: VoiceSession, user_text: str, speaker: str = "Participant"
    ) -> tuple[bool, str]:
        """
        Addressing gate:
        - Name said (incl. common Whisper misspellings) -> addressed.
        - Blocks 'Memorae' / 'Memo ray' / 'Boardy' collisions.
        - Conversation mode: while the SAME person who last talked with the bot keeps talking
          (no gap longer than follow_up_window_secs), un-named questions / "you" remarks count too.
          Other participants still have to say the name, so the bot doesn't butt into their talk.
        - Ignores all other speech and saves it as passive context.
        """
        text_lower = user_text.lower().strip()
        bot_name_lower = session.bot_name.lower().strip()

        if not bot_name_lower:
            return False, "no_bot_name"

        # Never answer other AI agents (Memorae agent, Boardy, notetakers): avoids bot-to-bot loops
        if speaker != "Participant" and self._is_ai_participant(session, speaker):
            return False, "ai_participant_speaking"

        # Conversation mode bookkeeping: the interlocutor speaking keeps the exchange alive.
        # Requires a KNOWN speaker: with unknown speakers everyone would look like the interlocutor.
        now = time.time()
        in_conversation = (
            speaker != "Participant"
            and session.last_interlocutor == speaker
            and now - session.conversation_active_at <= self.follow_up_window_secs
        )
        if in_conversation:
            session.conversation_active_at = now

        # Guard against Whisper splitting 'Memorae' into 'memo ray' / 'memo-re' / 'memo rae'
        sanitized_text = text_lower
        if bot_name_lower == "memo":
            sanitized_text = re.sub(r"\bmemo[\s\-]+(?:ray|re|rai|rae|ree|rey|rie)\b", "memorae", sanitized_text)

        # Map common Whisper misspellings of the name ("Mamo", "Mehmo") onto the real name
        variants = self._name_variants(bot_name_lower)
        if variants:
            variant_re = r"\b(?:" + "|".join(re.escape(v) for v in variants) + r")\b"
            sanitized_text = re.sub(variant_re, bot_name_lower, sanitized_text)

        # Require explicit whole-word mention of our bot's name
        name_re = re.escape(bot_name_lower)
        if re.search(rf"\b{name_re}\b", sanitized_text):
            # Vocative use ("Memo, ...", "..., Memo?", "hey Memo") is unambiguous direct address:
            # the LLM is NOT allowed to veto it with [SILENCE].
            words_only = re.sub(r"[^a-z0-9' ]", " ", sanitized_text).split()
            if (
                (words_only and (words_only[0] == bot_name_lower or words_only[-1] == bot_name_lower))
                or re.search(rf"\b(?:hey|hi|hello|ok|okay|yo|thanks|thank you)\s*,?\s*{name_re}\b", sanitized_text)
            ):
                return True, "direct_address"
            # Name mid-sentence ("I built memo with FastAPI") may be a third-person mention:
            # the LLM keeps a [SILENCE] veto for this case.
            return True, "name_mention"

        # Detect sibling bots explicitly for clearer terminal logs
        other_bots = [name for name in self.OTHER_BOT_NAMES if name != bot_name_lower]
        if other_bots:
            other_pattern = r"\b(?:" + "|".join(re.escape(n) for n in other_bots) + r")\b"
            if re.search(other_pattern, sanitized_text):
                return False, "other_bot_addressed"

        # Conversation mode: un-named question/remark from the person the bot is talking with.
        # If they name another participant ("Pau, can you see my screen?") it's not for us.
        if in_conversation:
            speaker_first = speaker.split()[0].lower() if speaker.split() else ""
            for other in session.participant_names:
                first = other.split()[0].lower() if other.split() else ""
                if len(first) >= 3 and first != speaker_first and re.search(rf"\b{re.escape(first)}\b", sanitized_text):
                    return False, f"addressed_to_{first}"
            if self._looks_like_follow_up(sanitized_text):
                return True, "follow_up"  # the LLM still gets a final [SILENCE] veto

        return False, "not_explicitly_addressed"

    @staticmethod
    def _calculate_rms(chunk: bytes) -> float:
        if not chunk or len(chunk) < 2:
            return 0.0
        count = len(chunk) // 2
        try:
            shorts = struct.unpack(f"<{count}h", chunk[:count * 2])
            sum_squares = sum(s * s for s in shorts)
            return (sum_squares / count) ** 0.5
        except Exception:
            return 0.0

    async def speak_text(self, session: VoiceSession, text: str) -> float:
        """
        Synthesizes text using Groq Orpheus TTS and streams 24kHz PCM packets
        into the meeting out_websocket in real-time.
        Returns TTS synthesis latency in seconds (0.0 on failure).
        """
        if not session.out_websocket:
            print(f"❌ [GOOGLE MEET] Cannot speak - no output socket connected (session {session.session_id})", flush=True)
            return 0.0

        async with session.speak_lock:
            return await self._speak_text_locked(session, text)

    FLOOR_WAIT_MAX_SECS: float = 3.0      # max time to wait for others to stop talking before speaking
    BARGE_IN_SECS: float = 1.0            # someone talking this long over the bot makes it stop

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Splits a reply into sentences for pipelined TTS; merges tiny fragments for natural prosody."""
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
        merged: List[str] = []
        for part in parts:
            if merged and len(merged[-1]) < 25:
                merged[-1] = f"{merged[-1]} {part}"
            else:
                merged.append(part)
        return merged or [text]

    async def _synthesize_pcm(self, text: str) -> bytes:
        tts_resp = await self._get_client().audio.speech.create(
            model="canopylabs/orpheus-v1-english",
            voice="autumn",
            input=text,
            response_format="wav"
        )
        wav_bytes = await tts_resp.read()
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return wf.readframes(wf.getnframes())

    async def _wait_for_floor(self, session: VoiceSession) -> None:
        """Polite turn-taking: if someone else just started talking, give them up to 3s to finish."""
        waited = 0.0
        announced = False
        while waited < self.FLOOR_WAIT_MAX_SECS:
            talking = self._others_speaking(session)
            if not talking and not session.user_is_speaking:
                return
            if not announced:
                who = ", ".join(talking) or "someone"
                print(f"⏸️ [FLOOR] {who} is talking - holding reply (max {self.FLOOR_WAIT_MAX_SECS:.0f}s)...", flush=True)
                announced = True
            await asyncio.sleep(0.1)
            waited += 0.1

    async def _speak_text_locked(self, session: VoiceSession, text: str) -> float:
        """
        Pipelined TTS: all sentences are synthesized concurrently and playback starts as soon as
        the FIRST sentence is ready. Waits for a free floor before speaking and stops if someone
        talks over it (barge-in). Returns time-to-first-audio-ready (TTS latency) in seconds.
        """
        sentences = self._split_sentences(text)
        logger.info(f"🔊 [Groq TTS] Synthesizing {len(sentences)} sentence(s): \"{text}\"")
        tts_start = time.time()
        tts_latency = 0.0
        # At most 3 TTS requests in flight: keeps playback fed without tripping Groq rate limits
        tts_slots = asyncio.Semaphore(3)

        async def synth_limited(sentence_text: str) -> bytes:
            async with tts_slots:
                return await self._synthesize_pcm(sentence_text)

        tasks = [asyncio.create_task(synth_limited(s)) for s in sentences]

        try:
            playback_started = 0.0
            for index, (sentence, task) in enumerate(zip(sentences, tasks)):
                pcm_data = await task
                if index == 0:
                    tts_latency = time.time() - tts_start
                    print(f"🔊 [GROQ ORPHEUS TTS] First sentence ready in {tts_latency:.2f}s "
                          f"({len(sentences)} sentence(s), pipelined)", flush=True)
                    await self._wait_for_floor(session)
                    session.is_speaking = True
                    playback_started = time.time()
                    print(f"▶️ [GOOGLE MEET] Speaking out loud to meeting: \"{text}\"", flush=True)

                # Stream in 20ms chunks (960 bytes at 24kHz 16-bit mono). Always write to the
                # *current* output socket so a mid-utterance reconnect keeps the audio going.
                chunk_size = self.chunk_size_bytes
                for offset in range(0, len(pcm_data), chunk_size):
                    ws = session.out_websocket
                    if ws is None:
                        print(f"❌ [GOOGLE MEET] Output socket lost mid-speech - meeting did not hear: \"{sentence}\"", flush=True)
                        return tts_latency
                    # Barge-in: someone who started talking AFTER we began, for >= 1s -> yield
                    interrupters = self._others_speaking(
                        session, started_after=playback_started, min_secs=self.BARGE_IN_SECS
                    )
                    if interrupters:
                        print(f"🛑 [BARGE-IN] {', '.join(interrupters)} started talking - stopping playback.", flush=True)
                        return tts_latency
                    await asyncio.wait_for(ws.send_bytes(pcm_data[offset:offset + chunk_size]), timeout=2.0)
                    await asyncio.sleep(0.02)  # 20ms real-time pacing

            logger.info(f"⏹️ Finished audio playback for: \"{text}\"")
            print(f"⏹️ [GOOGLE MEET] Finished audio playback.", flush=True)
            return tts_latency
        except Exception as e:
            logger.error(f"❌ Error in Groq TTS playback: {e}")
            print(f"❌ [GROQ TTS ERROR] {e}", flush=True)
            return tts_latency
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            session.is_speaking = False

    async def process_turn(self, session: VoiceSession, pcm_bytes: bytes, speaker: str = "Participant") -> None:
        """
        Executes full voice turn:
        1. Groq Whisper STT (whisper-large-v3-turbo)
        2. Address check / Conversation context
        3. Groq Qwen LLM (qwen/qwen3.8-27b)
        4. Groq Orpheus TTS (canopylabs/orpheus-v1-english)
        5. Real-time audio stream to Google Meet
        """
        client = self._get_client()
        session.turns_count += 1
        turn_num = session.turns_count
        turn_start = time.time()
        
        # 1. Package PCM into in-memory WAV for Whisper
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(session.channels)
            wf.setsampwidth(session.sample_width)
            wf.setframerate(session.sample_rate)
            wf.writeframes(pcm_bytes)
        wav_payload = wav_buf.getvalue()

        # 2. Whisper STT
        stt_start = time.time()
        logger.info(f"🎙️ [Turn #{turn_num}] Sending {len(wav_payload):,} audio bytes to Groq Whisper STT...")
        try:
            stt_result = await client.audio.transcriptions.create(
                file=("speech.wav", wav_payload, "audio/wav"),
                model="whisper-large-v3-turbo",
                language="en",
                temperature=0.0
            )
            stt_latency = time.time() - stt_start
            user_text = stt_result.text.strip()
            logger.info(f"📝 [Turn #{turn_num}] User said ({stt_latency:.2f}s): \"{user_text}\"")
            print(f"\n" + "=" * 60, flush=True)
            print(f"🎙️ [LIVE TURN #{turn_num}]", flush=True)
            print(f"📝 [GROQ WHISPER STT] {speaker}: \"{user_text}\" ({stt_latency:.2f}s)", flush=True)
        except Exception as stt_err:
            logger.error(f"❌ [Turn #{turn_num}] Whisper STT error: {stt_err}")
            print(f"❌ [GROQ STT ERROR] {stt_err}", flush=True)
            return

        normalized = re.sub(r"[^a-z ]", "", user_text.lower()).strip()
        if len(normalized) < 2 or normalized in self.WHISPER_HALLUCINATIONS:
            print(f"🔇 [Turn #{turn_num}] Ignored noise / Whisper hallucination: \"{user_text}\"", flush=True)
            return

        # Steps 3-5 run under the per-session turn lock: if turns pile up while the bot is
        # thinking or talking, they are answered one at a time instead of overlapping audio.
        if session.turn_lock.locked():
            print(f"⏳ [Turn #{turn_num}] Waiting for previous reply to finish...", flush=True)
        async with session.turn_lock:
            await self._respond_to_turn(session, turn_num, user_text, stt_latency, turn_start, speaker)

    async def _respond_to_turn(
        self, session: VoiceSession, turn_num: int, user_text: str, stt_latency: float, turn_start: float,
        speaker: str = "Participant"
    ) -> None:
        client = self._get_client()

        # 3. Multi-bot safe addressing gate
        is_addressed, gate_reason = self._evaluate_addressing(session, user_text, speaker)

        if not is_addressed:
            # Passively record into rolling in-meeting memory (never cleared during the call)
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

        # 4. Groq LLM Generation with Rolling In-Meeting Memory
        llm_start = time.time()
        must_answer = gate_reason == "direct_address"
        turn_note: Optional[str] = None
        if must_answer:
            turn_note = (
                "The next message speaks to you directly by name. You MUST answer it out loud. "
                "Do NOT reply [SILENCE]. If they ask you to remember something, briefly confirm "
                "you've got it."
            )
        elif gate_reason == "follow_up":
            turn_note = (
                "The next message did not say your name, but it comes from the person you are "
                "currently talking with. If it is a question or request, answer it naturally. If they "
                "are just sharing information with you, reply with a very brief acknowledgement "
                "(e.g. \"Got it.\") or [SILENCE]. Reply [SILENCE] if it is clearly directed at another person."
            )

        messages = session.memory.build_llm_messages(
            system_prompt=session.system_prompt,
            current_user_text=user_text,
            speaker=speaker,
            extra_system_note=turn_note,
        )
        
        model_name = "qwen/qwen3.8-27b"
        logger.info(
            f"🧠 [Turn #{turn_num}] Querying Groq LLM ({model_name}) "
            f"with {session.memory.utterance_count} rolling utterances, {len(session.memory.qa_history)} Q&A msgs..."
        )
        try:
            llm_resp = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=settings.llm_max_tokens,
                temperature=0.5
            )
            llm_latency = time.time() - llm_start
            raw_reply = (llm_resp.choices[0].message.content or "").strip()
            bot_reply = self._clean_for_speech(raw_reply)

            # Direct address can't be vetoed: if the model still tries, strip the token or fall back.
            if must_answer and (not bot_reply or "[SILENCE]" in bot_reply.upper()):
                stripped = re.sub(r"\[SILENCE\]", "", bot_reply, flags=re.IGNORECASE).strip()
                bot_reply = stripped or "Sorry, I didn't quite catch that. Could you say it again?"
                print(f"⚠️ [GROQ QWEN LLM]   Tried [SILENCE] on a direct address - overridden.", flush=True)

            # Honor explicit silence decision from system prompt (still keep utterance in memory!)
            if not bot_reply or "[SILENCE]" in bot_reply.upper():
                session.memory.add_room_utterance(user_text, speaker=speaker)
                logger.info(f"🤫 [Turn #{turn_num}] LLM chose [SILENCE] ({llm_latency:.2f}s) for: \"{user_text}\"")
                print(
                    f"🤫 [GROQ QWEN LLM]   Staying silent ({llm_latency:.2f}s) "
                    f"| 🧠 Memory: {session.memory.utterance_count} utterances",
                    flush=True
                )
                print("=" * 60 + "\n", flush=True)
                return

            logger.info(f"💡 [Turn #{turn_num}] Groq LLM reply ({llm_latency:.2f}s): \"{bot_reply}\"")
            print(f"🧠 [GROQ QWEN LLM]   {session.bot_name}: \"{bot_reply}\" ({llm_latency:.2f}s)", flush=True)
            
            # Record exchange into rolling timeline + Q&A history (never wipes earlier meeting memory)
            session.memory.add_exchange(user_text, bot_reply, speaker=speaker)
            print(
                f"🗂️ [MEETING MEMORY]  {session.memory.utterance_count} rolling utterances | "
                f"{len(session.memory.qa_history)} Q&A msgs",
                flush=True
            )
        except Exception as llm_err:
            logger.error(f"❌ [Turn #{turn_num}] Groq LLM error: {llm_err}")
            print(f"❌ [GROQ LLM ERROR] {llm_err}", flush=True)
            return

        # 5. Groq TTS Playback
        tts_latency = await self.speak_text(session, bot_reply)
        session.last_bot_spoke_at = time.time()
        # Start/extend conversation mode with whoever we just answered
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

    def _handle_stream_event(self, session: VoiceSession, text_data: str) -> None:
        """
        Parses JSON text frames on the audio-in socket:
        - handshake dict ({"protocol_version", "bot_id", ...}) -> remember bot_id
        - speaker list ([{"name", "isSpeaking", ...}, ...]) -> track the active human speaker
        """
        try:
            payload = json.loads(text_data)
        except ValueError:
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

            # Track when each participant started speaking (used for floor control / barge-in).
            # Meet's indicator flickers between words, so a speaker only "stops" after
            # SPEAKING_GRACE_SECS without the indicator; short gaps keep the same speech run.
            for name in speaking_now:
                last_seen = session.speaking_last_seen.get(name)
                if name not in session.speaking_since or last_seen is None or now - last_seen > self.SPEAKING_GRACE_SECS:
                    session.speaking_since[name] = now
                    tag = " (AI)" if self._is_ai_participant(session, name) else ""
                    print(f"🗣️ [SPEAKER] {name}{tag} is speaking", flush=True)
                session.speaking_last_seen[name] = now
            self._expire_speakers(session, now)

            if speaking_now:
                # Most recent speaker = current one; first speaker during a segment owns it
                newest = max(speaking_now, key=lambda n: session.speaking_since[n])
                session.active_speaker = newest
                if session.user_is_speaking and not session.segment_speaker:
                    session.segment_speaker = newest

    def _is_ai_participant(self, session: VoiceSession, name: str) -> bool:
        """Other AI agents / notetakers in the call (by display name)."""
        lowered = name.lower()
        if lowered == session.bot_name.lower().strip():
            return False
        keywords = list(self.OTHER_BOT_NAMES) + [
            k.strip().lower() for k in settings.other_ai_participants.split(",") if k.strip()
        ]
        if any(k in lowered for k in keywords):
            return True
        return bool(re.search(
            r"\b(?:bot|ai|notetaker|note taker|assistant|recorder|fireflies|otter|fathom|tldv|read\.ai)\b",
            lowered,
        ))

    def _others_speaking(self, session: VoiceSession, started_after: float = 0.0, min_secs: float = 0.0) -> list[str]:
        """
        Participants currently speaking (per Meet's speaking indicators), excluding this bot.
        Ignores indicators stuck on for >15s (noisy open mics) so they can't block the bot forever.
        """
        now = time.time()
        self._expire_speakers(session, now)
        talking = []
        for name, since in session.speaking_since.items():
            heard_for = session.speaking_last_seen.get(name, since) - since  # actual observed speech run
            if since >= started_after and heard_for >= min_secs and now - since <= 15.0:
                talking.append(name)
        return talking

    SPEAKING_GRACE_SECS: float = 0.8   # indicator gaps shorter than this don't end a speech run

    def _expire_speakers(self, session: VoiceSession, now: float) -> None:
        """Drops speakers whose indicator has been off longer than the grace period."""
        for name in list(session.speaking_since):
            last_seen = session.speaking_last_seen.get(name, 0.0)
            if now - last_seen > self.SPEAKING_GRACE_SECS:
                del session.speaking_since[name]
                session.speaking_last_seen.pop(name, None)

    async def handle_audio_in(self, session: VoiceSession, websocket: WebSocket) -> None:
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
                        self._handle_stream_event(session, text_data)
                    continue
                
                session.frames_received += 1
                rms = self._calculate_rms(data)
                if session.frames_received == 1:
                    print(f"🎵 [TERMINAL] First audio packet received from Google Meet! ({len(data)} bytes, RMS: {rms:.0f})", flush=True)

                # Heartbeat every 10s so "bot can't hear" is diagnosable from the terminal
                hb_frames += 1
                hb_peak = max(hb_peak, rms)
                if time.time() - hb_started >= 10.0:
                    print(
                        f"🎚️ [AUDIO-IN] {hb_frames} packets in last 10s | peak RMS {hb_peak:.0f} "
                        f"(speech threshold {self.silence_threshold_rms:.0f}) | bot speaking: {session.is_speaking} "
                        f"| output socket: {'OK' if session.out_websocket else 'MISSING'}",
                        flush=True
                    )
                    hb_started, hb_frames, hb_peak = time.time(), 0, 0.0

                # If bot is currently speaking, mute/ignore mic audio to prevent acoustic echo
                if session.is_speaking:
                    continue

                is_speech = rms > self.silence_threshold_rms

                async with session.lock:
                    if is_speech:
                        if not session.user_is_speaking:
                            print(f"👂 [TERMINAL] Hearing speech in Google Meet... (RMS: {rms:.0f})", flush=True)
                            session.segment_speaker = None  # filled by speaker events during this segment
                        session.speech_bytes += len(data)
                        session.silence_bytes = 0
                        session.user_is_speaking = True
                        session.audio_buffer.extend(data)
                    elif session.user_is_speaking:
                        # User was speaking, now silence
                        session.silence_bytes += len(data)
                        session.audio_buffer.extend(data)

                    # Check if turn should fire (0.5s silence OR max buffer cap reached)
                    should_trigger = (
                        session.user_is_speaking
                        and (
                            session.silence_bytes >= self.silence_bytes_trigger
                            or len(session.audio_buffer) >= self.max_speech_bytes
                        )
                    )

                    if should_trigger:
                        if session.speech_bytes >= self.min_speech_bytes:
                            speech_pcm = bytes(session.audio_buffer)
                            session.audio_buffer.clear()
                            session.user_is_speaking = False
                            session.speech_bytes = 0
                            session.silence_bytes = 0
                            
                            # Attribute the segment to whoever Meet reported speaking during it
                            speaker = session.segment_speaker or session.active_speaker or "Participant"
                            session.segment_speaker = None
                            asyncio.create_task(self.process_turn(session, speech_pcm, speaker))
                        else:
                            # Too short, discard as noise
                            session.audio_buffer.clear()
                            session.user_is_speaking = False
                            session.speech_bytes = 0
                            session.silence_bytes = 0

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"❌ Exception in inbound audio stream: {e}")
        finally:
            if session.in_websocket is websocket:
                session.in_websocket = None
            print(f"🔌 [TERMINAL] Audio INPUT socket closed (session {session.session_id}); "
                  f"active input socket remaining: {session.in_websocket is not None}", flush=True)

    async def handle_audio_out(self, session: VoiceSession, websocket: WebSocket) -> None:
        """
        Handles Meeting BaaS input_url connection. When connected, greets the call,
        then remains open to receive synthesized bot speech.
        """
        session.out_websocket = websocket
        logger.info(f"📢 Outbound audio stream connected for session {session.session_id}")
        print(f"\n📢 [TERMINAL] Google Meet Audio Output Connected! Bot microphone active.", flush=True)
        
        # Greet once per session, only after the bot is admitted (runs in background so the
        # socket keeps being serviced while we wait in the waiting room).
        greet_task = asyncio.create_task(self._greet_when_admitted(session))

        try:
            # Keep socket open and listen for any peer pings/events
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"❌ Exception in outbound audio stream: {e}")
        finally:
            # Only clear the reference if it still points at THIS socket. Meeting BaaS reconnects
            # streams (e.g. after admission); the old socket closing must not kill the new one.
            if session.out_websocket is websocket:
                session.out_websocket = None
            print(f"🔌 [TERMINAL] Audio OUTPUT socket closed (session {session.session_id}); "
                  f"active output socket remaining: {session.out_websocket is not None}", flush=True)
            if not greet_task.done() and session.out_websocket is None:
                greet_task.cancel()

    async def _wait_until_admitted(self, session: VoiceSession, timeout_secs: float = 180.0) -> bool:
        """Polls the provider status until the bot is in the call. Returns False if it ended/failed."""
        if not session.bot_id:
            return True  # ad-hoc session without a known bot: don't block the greeting
        from app.services.meeting_baas import meeting_baas_service  # local import: avoid cycle

        deadline = time.time() + timeout_secs
        announced = False
        while time.time() < deadline:
            try:
                status = (await asyncio.to_thread(meeting_baas_service.get_status, session.bot_id)).status
            except Exception as e:
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

    async def _greet_when_admitted(self, session: VoiceSession) -> None:
        if session.greeted or not session.entry_message:
            return
        if not await self._wait_until_admitted(session):
            return
        await asyncio.sleep(1.0)  # let the meeting audio path settle after admission
        # Check-and-set with no await in between: two concurrent handlers can't both greet.
        if session.greeted or not session.out_websocket:
            return
        session.greeted = True
        await self.speak_text(session, session.entry_message)

groq_voice_pipeline = GroqVoicePipelineManager()
