import asyncio
import io
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

class VoiceSession:
    def __init__(self, session_id: str, bot_name: str, system_prompt: str, entry_message: str):
        self.session_id: str = session_id
        self.bot_name: str = bot_name
        self.system_prompt: str = system_prompt
        self.entry_message: str = entry_message
        
        # Audio & WebSocket channels
        self.in_websocket: Optional[WebSocket] = None
        self.out_websocket: Optional[WebSocket] = None
        
        # Conversation & room context
        self.conversation_history: List[Dict[str, str]] = []
        self.recent_room_context: List[str] = []
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

        # Diagnostics
        self.turns_count: int = 0
        self.total_speech_secs: float = 0.0

class GroqVoicePipelineManager:
    # Common Whisper transcriptions of sibling bots in multi-bot Google Meets
    OTHER_BOT_NAMES: tuple[str, ...] = ("memorae", "boardy", "bordy", "boardie")
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
    })

    def __init__(self):
        self.sessions: Dict[str, VoiceSession] = {}
        self.groq_client: Optional[AsyncGroq] = None
        
        # VAD Tuning Constants (at 24kHz mono 16-bit = 48,000 bytes/sec)
        self.chunk_size_bytes: int = 960            # 20ms playback chunk
        self.bytes_per_second: int = 48000          # 24000 Hz * 2 bytes
        self.silence_threshold_rms: float = 300.0   # Sensitive speech detection
        self.silence_bytes_trigger: int = 36000     # 0.75s silence pause prevents mid-sentence cutoffs
        self.min_speech_bytes: int = 14400          # 0.30s min speech to avoid pops
        self.max_speech_bytes: int = 480000         # 10.0s max buffer cap
        self.follow_up_window_secs: float = 18.0    # Follow-up window after bot replies

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

    def _looks_like_follow_up(self, text_lower: str) -> bool:
        """Heuristic: is this a question/request plausibly continuing the bot's last answer?"""
        stripped = text_lower.strip().rstrip(".!")
        if not stripped or stripped in self.BACKCHANNEL_PHRASES:
            return False
        if stripped.endswith("?"):
            return True
        stripped = re.sub(r"^(?:and|so|but|okay|ok|alright|then)\b[,\s]*", "", stripped)
        words = stripped.split()
        return bool(words) and words[0].strip(",") in self.QUESTION_STARTERS

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

    def _evaluate_addressing(self, session: VoiceSession, user_text: str) -> tuple[bool, str]:
        """
        Strict explicit-address gate:
        - ONLY responds when a participant explicitly calls this bot by its exact name.
        - Blocks 'Memorae' / 'Memo ray' / 'Boardy' collisions.
        - Within follow_up_window_secs after the bot spoke, also accepts un-named questions.
        - Ignores all other speech and saves it as passive context.
        """
        text_lower = user_text.lower().strip()
        bot_name_lower = session.bot_name.lower().strip()

        if not bot_name_lower:
            return False, "no_bot_name"

        # Guard against Whisper splitting 'Memorae' into 'memo ray' / 'memo re'
        sanitized_text = text_lower
        if bot_name_lower == "memo":
            sanitized_text = re.sub(r"\bmemo\s+(?:ray|re|rai|rae)\b", "memorae", sanitized_text)

        # Require explicit whole-word mention of our bot's name
        if re.search(rf"\b{re.escape(bot_name_lower)}\b", sanitized_text):
            return True, "explicit_name"

        # Detect sibling bots explicitly for clearer terminal logs
        other_bots = [name for name in self.OTHER_BOT_NAMES if name != bot_name_lower]
        if other_bots:
            other_pattern = r"\b(?:" + "|".join(re.escape(n) for n in other_bots) + r")\b"
            if re.search(other_pattern, sanitized_text):
                return False, "other_bot_addressed"

        # Follow-up window: shortly after the bot answered, a question without the name
        # is most likely aimed at the bot. The LLM still gets a final [SILENCE] veto.
        if (
            session.last_bot_spoke_at
            and time.time() - session.last_bot_spoke_at <= self.follow_up_window_secs
            and self._looks_like_follow_up(sanitized_text)
        ):
            return True, "follow_up"

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

    async def _speak_text_locked(self, session: VoiceSession, text: str) -> float:
        client = self._get_client()
        logger.info(f"🔊 [Groq TTS] Synthesizing speech for: \"{text}\"")
        tts_start = time.time()
        tts_latency = 0.0
        
        try:
            tts_resp = await client.audio.speech.create(
                model="canopylabs/orpheus-v1-english",
                voice="autumn",
                input=text,
                response_format="wav"
            )
            wav_bytes = await tts_resp.read()
            tts_latency = time.time() - tts_start
            logger.info(f"✅ [Groq TTS] Generated {len(wav_bytes):,} bytes in {tts_latency:.2f}s")
            print(f"🔊 [GROQ ORPHEUS TTS] Generated speech in {tts_latency:.2f}s ({len(wav_bytes):,} bytes)", flush=True)

            # Extract raw PCM frames from WAV
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                pcm_data = wf.readframes(wf.getnframes())

            session.is_speaking = True
            logger.info(f"▶️ Streaming {len(pcm_data):,} PCM bytes to Google Meet audio output...")
            print(f"▶️ [GOOGLE MEET] Speaking out loud to meeting: \"{text}\"", flush=True)
            
            # Stream in 20ms chunks (960 bytes at 24kHz 16-bit mono). Always write to the
            # *current* output socket so a mid-utterance reconnect keeps the audio going.
            chunk_size = self.chunk_size_bytes
            for offset in range(0, len(pcm_data), chunk_size):
                chunk = pcm_data[offset:offset + chunk_size]
                ws = session.out_websocket
                if ws is None:
                    print(f"❌ [GOOGLE MEET] Output socket lost mid-speech - meeting did not hear: \"{text}\"", flush=True)
                    return tts_latency
                await asyncio.wait_for(ws.send_bytes(chunk), timeout=2.0)
                await asyncio.sleep(0.02)  # 20ms real-time pacing

            logger.info(f"⏹️ Finished audio playback for: \"{text}\"")
            print(f"⏹️ [GOOGLE MEET] Finished audio playback.", flush=True)
            return tts_latency
        except Exception as e:
            logger.error(f"❌ Error in Groq TTS playback: {e}")
            print(f"❌ [GROQ TTS ERROR] {e}", flush=True)
            return tts_latency
        finally:
            session.is_speaking = False

    async def process_turn(self, session: VoiceSession, pcm_bytes: bytes) -> None:
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
            print(f"📝 [GROQ WHISPER STT] User: \"{user_text}\" ({stt_latency:.2f}s)", flush=True)
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
            await self._respond_to_turn(session, turn_num, user_text, stt_latency, turn_start)

    async def _respond_to_turn(
        self, session: VoiceSession, turn_num: int, user_text: str, stt_latency: float, turn_start: float
    ) -> None:
        client = self._get_client()

        # 3. Multi-bot safe addressing gate
        is_addressed, gate_reason = self._evaluate_addressing(session, user_text)

        if not is_addressed:
            # Passively remember last 6 overheard lines so the bot keeps meeting context
            session.recent_room_context.append(user_text)
            if len(session.recent_room_context) > 6:
                session.recent_room_context = session.recent_room_context[-6:]

            logger.info(f"🤫 [Turn #{turn_num}] Staying silent ({gate_reason}): \"{user_text}\"")
            print(f"🤫 [ADDRESSING GATE] Staying silent ({gate_reason})", flush=True)
            print("=" * 60 + "\n", flush=True)
            return

        logger.info(f"🎯 [Turn #{turn_num}] Addressing gate passed ({gate_reason})")
        print(f"🎯 [ADDRESSING GATE] Engaged ({gate_reason})", flush=True)

        # 4. Groq LLM Generation
        llm_start = time.time()
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": session.system_prompt},
        ]
        # Include any recent passively overheard room context
        if session.recent_room_context:
            ambient_summary = " | ".join(session.recent_room_context)
            messages.append({
                "role": "system",
                "content": f"Recent background meeting context you passively heard: {ambient_summary}"
            })
        messages.extend(session.conversation_history[-8:])
        if gate_reason == "follow_up":
            messages.append({
                "role": "system",
                "content": (
                    "The next message did not say your name, but it came right after your last answer "
                    "and looks like a follow-up question to you. Answer it naturally. Reply [SILENCE] "
                    "only if it is clearly directed at another person."
                )
            })
        messages.append({"role": "user", "content": user_text})
        
        model_name = "qwen/qwen3.8-27b"
        logger.info(f"🧠 [Turn #{turn_num}] Querying Groq LLM ({model_name})...")
        try:
            llm_resp = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=120,
                temperature=0.5
            )
            llm_latency = time.time() - llm_start
            raw_reply = (llm_resp.choices[0].message.content or "").strip()
            bot_reply = self._clean_for_speech(raw_reply)

            # Honor explicit silence decision from system prompt
            if not bot_reply or "[SILENCE]" in bot_reply.upper():
                logger.info(f"🤫 [Turn #{turn_num}] LLM chose [SILENCE] ({llm_latency:.2f}s) for: \"{user_text}\"")
                print(f"🤫 [GROQ QWEN LLM]   Staying silent ({llm_latency:.2f}s)", flush=True)
                print("=" * 60 + "\n", flush=True)
                return

            logger.info(f"💡 [Turn #{turn_num}] Groq LLM reply ({llm_latency:.2f}s): \"{bot_reply}\"")
            print(f"🧠 [GROQ QWEN LLM]   {session.bot_name}: \"{bot_reply}\" ({llm_latency:.2f}s)", flush=True)
            
            # Update history and clear consumed room context
            session.recent_room_context.clear()
            session.conversation_history.append({"role": "user", "content": user_text})
            session.conversation_history.append({"role": "assistant", "content": bot_reply})
        except Exception as llm_err:
            logger.error(f"❌ [Turn #{turn_num}] Groq LLM error: {llm_err}")
            print(f"❌ [GROQ LLM ERROR] {llm_err}", flush=True)
            return

        # 5. Groq TTS Playback
        tts_latency = await self.speak_text(session, bot_reply)
        session.last_bot_spoke_at = time.time()
        
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
                        print(f"ℹ️ [AUDIO-IN TEXT] {text_data[:120]}", flush=True)
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
                            
                            # Launch turn processing asynchronously
                            asyncio.create_task(self.process_turn(session, speech_pcm))
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
