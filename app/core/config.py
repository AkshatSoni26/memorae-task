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
        self.llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "150"))

        # Bot Persona & Behavior Defaults
        self.default_bot_name: str = os.getenv("DEFAULT_BOT_NAME", "memo")
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
