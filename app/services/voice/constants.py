import re
from pathlib import Path
from app.core.config import settings

# Paths
SESSIONS_FILE: Path = Path(__file__).resolve().parents[3] / ".runtime" / "voice_sessions.json"

# Audio & VAD defaults (at 24kHz mono 16-bit = 48,000 bytes/sec)
CHUNK_SIZE_BYTES: int = 960            # 20ms playback chunk
BYTES_PER_SECOND: int = 48000          # 24000 Hz * 2 bytes
DEFAULT_SILENCE_THRESHOLD_RMS: float = settings.vad_silence_threshold_rms
DEFAULT_SILENCE_BYTES_TRIGGER: int = settings.vad_silence_bytes_trigger
DEFAULT_MIN_SPEECH_BYTES: int = settings.vad_min_speech_bytes
DEFAULT_MAX_SPEECH_BYTES: int = settings.vad_max_speech_bytes
DEFAULT_FOLLOW_UP_WINDOW_SECS: float = settings.follow_up_window_secs

# Floor & Turn-taking
FLOOR_WAIT_MAX_SECS: float = settings.floor_wait_max_secs
BARGE_IN_SECS: float = settings.barge_in_secs
SPEAKING_GRACE_SECS: float = settings.speaking_grace_secs      # indicator gaps shorter than this keep speech run
AI_REPLY_STREAK_MAX: int = settings.ai_reply_streak_max          # max consecutive replies to AI agents

# URLs configured in Settings
DEEPGRAM_SPEAK_URL: str = settings.deepgram_speak_url
DEEPGRAM_LISTEN_URL: str = settings.deepgram_listen_url

# Speech heuristics
BACKCHANNEL_PHRASES: frozenset[str] = frozenset({
    "ok", "okay", "k", "thanks", "thank you", "cool", "nice", "great",
    "got it", "yeah", "yes", "yep", "nope", "alright", "all right",
    "uh-huh", "mm-hmm", "hmm", "right", "sure", "awesome", "perfect",
})

WHISPER_HALLUCINATIONS: frozenset[str] = frozenset({
    "you", "thank you", "thanks", "thank you so much", "thanks for watching",
    "thank you for watching", "bye", "bye bye", "uh", "um", "hmm", "so",
})

QUESTION_STARTERS: frozenset[str] = frozenset({
    "what", "why", "how", "when", "where", "who", "whom", "whose", "which",
    "can", "could", "would", "should", "will", "is", "are", "was", "were",
    "do", "does", "did", "have", "has", "tell", "explain", "give", "say",
    "let", "repeat", "remind", "remember",
})

LEADING_FILLERS_RE = re.compile(
    r"^(?:(?:and|so|but|okay|ok|alright|all right|then|now|yeah|yes|yep|well|um|uh|hmm|right)\b[,.\s]*)+"
)

GROUP_ADDRESS_RE = re.compile(
    r"\b(?:everyone|everybody|all of you|both of you|you all|y'all|you guys|yourselves|each of you"
    r"|(?:the |all the |both )?(?:bots|agents|ais|assistants))\b"
)
