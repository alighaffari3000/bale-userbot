"""Gemini provider.

Kept behind a tiny interface (`LLM.reply`) so a second provider can be added
later without touching the message handler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from google import genai
from google.genai import types

from .config import Config
from .storage import Turn

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when the model could not produce a usable answer."""


class GeminiLLM:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = genai.Client(api_key=config.gemini_api_key)
        self._generate_config = types.GenerateContentConfig(
            system_instruction=config.system_prompt,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
        )

    @staticmethod
    def _contents(history: Sequence[Turn], prompt: str) -> list[types.Content]:
        contents = [
            types.Content(role=turn.role, parts=[types.Part.from_text(text=turn.text)])
            for turn in history
            if turn.text.strip()
        ]
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        )
        return contents

    async def reply(
        self,
        prompt: str,
        history: Sequence[Turn] = (),
        *,
        attempts: int = 3,
    ) -> str:
        """Generate one answer, retrying transient API failures with backoff."""
        contents = self._contents(history, prompt)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._config.gemini_model,
                    contents=contents,
                    config=self._generate_config,
                )
            except Exception as exc:  # network / quota / server errors
                last_error = exc
                if attempt == attempts:
                    break
                delay = 2 ** attempt
                logger.warning(
                    "Gemini call failed (attempt %s/%s), retrying in %ss: %s",
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue

            text = (response.text or "").strip()
            if text:
                return text

            # Empty output usually means a safety block or a truncated response.
            reason = None
            if getattr(response, "candidates", None):
                reason = getattr(response.candidates[0], "finish_reason", None)
            raise LLMError(f"model returned no text (finish_reason={reason})")

        raise LLMError(f"model call failed after {attempts} attempts: {last_error}")
