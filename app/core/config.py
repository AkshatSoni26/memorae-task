import os
from dotenv import load_dotenv

# Ensure .env is explicitly loaded into environment
load_dotenv(override=True)

class Settings:
    def __init__(self):
        # Credentials & API URLs
        self.meeting_baas_api_key: str = (
            os.getenv("MEETING_BAAS")
            or os.getenv("MEETING_BAAS_API_KEY")
            or ""
        )
        self.meeting_baas_webhook_secret: str = (
            os.getenv("MEETING_BAAS_WEBHOOK")
            or os.getenv("MEETING_BAAS_WEBHOOK_SECRET")
            or ""
        )
        self.speaking_api_url: str = os.getenv("SPEAKING_API_URL", "https://speaking.meetingbaas.com/bots")
        self.baas_api_url: str = os.getenv("BAAS_API_URL", "https://api.meetingbaas.com/bots")
        self.webhook_url: str = os.getenv("WEBHOOK_URL", "")

        # Groq API Configuration
        self.groq_api_key: str = os.getenv("GROQ_API_KEY", "")

        # LiteLLM Model Configuration
        self.llm_model: str = os.getenv("LLM_MODEL", "groq/qwen/qwen3.8-27b")
        self.llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
        self.llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "400"))
        # Speech engines: USE_GROQ=true -> Groq Whisper STT + Groq Orpheus TTS.
        # Otherwise -> Deepgram Nova-3 STT + Deepgram Flux TTS. No fallback between them.
        # (The LLM is always Groq.)
        self.use_groq: bool = os.getenv("USE_GROQ", "false").strip().lower() in ("1", "true", "yes", "on")
        self.deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
        self.deepgram_tts_model: str = os.getenv("DEEPGRAM_TTS_MODEL", "flux-sienna-en")
        self.deepgram_stt_model: str = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
        self.deepgram_speak_url: str = os.getenv("DEEPGRAM_SPEAK_URL", "https://api.deepgram.com/v2/speak")
        self.deepgram_listen_url: str = os.getenv("DEEPGRAM_LISTEN_URL", "https://api.deepgram.com/v1/listen")
        self.deepgram_encoding: str = os.getenv("DEEPGRAM_ENCODING", "linear16")
        self.deepgram_container: str = os.getenv("DEEPGRAM_CONTAINER", "none")
        self.deepgram_smart_format: bool = os.getenv("DEEPGRAM_SMART_FORMAT", "true").strip().lower() in ("1", "true", "yes", "on")
        self.deepgram_punctuate: bool = os.getenv("DEEPGRAM_PUNCTUATE", "true").strip().lower() in ("1", "true", "yes", "on")
        self.stt_language: str = os.getenv("STT_LANGUAGE", "en")
        self.stt_prompt_prefix: str = os.getenv("STT_PROMPT_PREFIX", "Google Meet call with ")
        self.stt_max_keyterms: int = int(os.getenv("STT_MAX_KEYTERMS", "50"))
        self.stt_max_prompt_participants: int = int(os.getenv("STT_MAX_PROMPT_PARTICIPANTS", "20"))
        self.stt_max_prompt_chars: int = int(os.getenv("STT_MAX_PROMPT_CHARS", "600"))
        self.speech_provider: str = "groq" if self.use_groq else "deepgram"

        # Groq Voice Pipeline Configuration
        self.groq_tts_model: str = os.getenv("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
        self.groq_tts_voice: str = os.getenv("GROQ_TTS_VOICE", "autumn")
        self.groq_tts_response_format: str = os.getenv("GROQ_TTS_RESPONSE_FORMAT", "wav")
        self.groq_stt_model: str = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
        self.groq_stt_temperature: float = float(os.getenv("GROQ_STT_TEMPERATURE", "0.0"))
        self.groq_llm_model: str = os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b")

        # Audio Stream Specifications
        self.audio_sample_rate: int = int(os.getenv("AUDIO_SAMPLE_RATE", "24000"))
        self.audio_sample_width: int = int(os.getenv("AUDIO_SAMPLE_WIDTH", "2"))
        self.audio_channels: int = int(os.getenv("AUDIO_CHANNELS", "1"))
        self.stt_sample_rate: int = int(os.getenv("STT_SAMPLE_RATE", "16000"))

        # Voice Activity Detection (VAD) & Turn-Taking Tuning
        self.vad_silence_threshold_rms: float = float(os.getenv("VAD_SILENCE_THRESHOLD_RMS", "300.0"))
        self.vad_silence_bytes_trigger: int = int(os.getenv("VAD_SILENCE_BYTES_TRIGGER", "36000"))
        self.vad_min_speech_bytes: int = int(os.getenv("VAD_MIN_SPEECH_BYTES", "14400"))
        self.vad_max_speech_bytes: int = int(os.getenv("VAD_MAX_SPEECH_BYTES", "960000"))
        self.floor_wait_max_secs: float = float(os.getenv("FLOOR_WAIT_MAX_SECS", "3.0"))
        self.barge_in_secs: float = float(os.getenv("BARGE_IN_SECS", "1.0"))
        self.speaking_grace_secs: float = float(os.getenv("SPEAKING_GRACE_SECS", "0.8"))
        self.ai_reply_streak_max: int = int(os.getenv("AI_REPLY_STREAK_MAX", "2"))
        self.follow_up_window_secs: float = float(os.getenv("FOLLOW_UP_WINDOW_SECS", "30.0"))
        self.admission_wait_timeout_secs: float = float(os.getenv("ADMISSION_WAIT_TIMEOUT_SECS", "180.0"))
        self.ws_send_timeout_secs: float = float(os.getenv("WS_SEND_TIMEOUT_SECS", "2.0"))

        # Bot Persona & Behavior Defaults
        self.default_bot_name: str = os.getenv("DEFAULT_BOT_NAME", "Jarvis")
        # Extra comma-separated spellings the transcriber may produce for the bot name
        self.bot_name_aliases: str = os.getenv("BOT_NAME_ALIASES", "")
        # Extra comma-separated display-name keywords of OTHER AI agents in the call (never answered)
        # Other AI agents expected in the call (comma-separated display names). Used to apply the
        # bot-to-bot rules when they speak, and to stay silent when someone talks to them.
        self.other_ai_participants: str = os.getenv("OTHER_AI_PARTICIPANTS", "Memorae,Boardy")
        self.default_prompt: str = os.getenv(
            "DEFAULT_BOT_PROMPT",
            (
                f"You are {self.default_bot_name}, a friendly AI participant in a Google Meet. "
                "Always listen carefully and answer out loud whenever anyone speaks to you or asks you a question. "
                "Keep answers very concise, direct, conversational, and natural (1-2 sentences). "
                "Always answer questions directly."
            )
        )
        self.default_entry_message: str = os.getenv(
            "DEFAULT_ENTRY_MESSAGE",
            f"Hey everyone, {self.default_bot_name} here. Glad to join."
        )
        self.default_recording_mode: str = os.getenv("DEFAULT_RECORDING_MODE", "speaker_view")
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")

settings = Settings()
