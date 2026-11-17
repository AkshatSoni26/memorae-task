from typing import List, Dict, Optional
import litellm
from app.core.config import settings
from app.core.logger import logger
from app.prompts import load_system_prompt

# Suppress noisy telemetry and banner from litellm
litellm.telemetry = False
litellm.suppress_debug_info = True

class AgentBrainService:
    def __init__(self):
        self.model: str = settings.llm_model
        self.temperature: float = settings.llm_temperature
        self.max_tokens: int = settings.llm_max_tokens

    def is_addressed_to_bot(self, speech_text: str, bot_name: Optional[str] = None) -> bool:
        """
        Determines whether the transcribed speech was directed at the bot.
        """
        target_name = (bot_name or settings.default_bot_name).lower()
        cleaned_text = speech_text.lower().strip()

        # Check for direct name mentions or common questions
        if target_name in cleaned_text:
            logger.info(f"🎯 Name trigger detected for '{target_name}' in: \"{speech_text}\"")
            return True

        logger.debug(f"🔇 Speech not addressed to '{target_name}': \"{speech_text}\"")
        return False

    def generate_response(
        self,
        transcript_history: List[Dict[str, str]],
        custom_system_prompt: Optional[str] = None,
        custom_model: Optional[str] = None
    ) -> Optional[str]:
        """
        Calls LiteLLM to generate an intelligent conversational response.
        Works seamlessly with groq/..., openai/..., anthropic/..., gemini/..., etc.
        """
        system_prompt = custom_system_prompt or load_system_prompt()
        model_name = custom_model or self.model
        messages = [{"role": "system", "content": system_prompt}] + transcript_history

        logger.info(f"🧠 Querying LiteLLM (model: '{model_name}') with {len(messages)} messages...")

        try:
            response = litellm.completion(
                model=model_name,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            answer = response.choices[0].message.content.strip()
            logger.info(f"✅ LiteLLM response generated ({len(answer)} chars): \"{answer}\"")
            return answer

        except litellm.exceptions.AuthenticationError as auth_err:
            logger.error(f"❌ LiteLLM Authentication error: {auth_err}")
            return None
        except litellm.exceptions.RateLimitError as rate_err:
            logger.error(f"❌ LiteLLM Rate limit reached: {rate_err}")
            return None
        except litellm.exceptions.Timeout as timeout_err:
            logger.error(f"⏱️ LiteLLM Request timed out: {timeout_err}")
            return None
        except litellm.exceptions.APIError as api_err:
            logger.error(f"❌ LiteLLM API error: {api_err}")
            return None

agent_brain_service = AgentBrainService()
