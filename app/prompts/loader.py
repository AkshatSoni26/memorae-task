from pathlib import Path
from app.core.config import settings
from app.core.logger import logger

PROMPTS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_PROMPT_FILE: Path = PROMPTS_DIR / "system_prompt.md"

def load_system_prompt(
    bot_name: str | None = None,
    prompt_path: Path | None = None,
    include_silence_token: bool = True
) -> str:
    """
    Loads the voice agent system prompt from Markdown file and injects {bot_name}.
    Reads fresh from disk so prompt edits take effect immediately on the next session.
    When include_silence_token is False (e.g. hosted Speaking Bots without a [SILENCE]
    interceptor), strips [SILENCE] protocol lines so TTS never speaks the token aloud.
    """
    resolved_name: str = (bot_name or settings.default_bot_name).strip()
    target_path: Path = prompt_path or DEFAULT_PROMPT_FILE

    try:
        raw_content: str = target_path.read_text(encoding="utf-8").strip()
        if not include_silence_token:
            filtered_lines: list[str] = [
                line for line in raw_content.splitlines()
                if "[SILENCE]" not in line and "exact token" not in line.lower()
            ]
            raw_content = "\n".join(filtered_lines).strip()

        rendered: str = raw_content.replace("{bot_name}", resolved_name)
        logger.info(
            f"📄 Loaded system prompt from {target_path.name} "
            f"({len(rendered)} chars, silence_token={include_silence_token}) for '{resolved_name}'"
        )
        return rendered
    except OSError as file_err:
        logger.warning(f"⚠️ Could not read {target_path}, falling back to default settings prompt: {file_err}")
        return settings.default_prompt
