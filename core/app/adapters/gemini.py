"""Google Gemini adapter (OpenAI-compatible endpoint).

API: https://generativelanguage.googleapis.com/v1beta/openai/
Key: GEMINI_API_KEY in secrets/providers.env
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-2.5-flash"
TIMEOUT = 30.0


class GeminiAdapter:
    def __init__(self):
        self._api_key = os.environ.get("GEMINI_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def generate(
        self,
        prompt: str,
        model: str = DEFAULT_MODEL,
        system: str = "You are a helpful assistant.",
    ) -> dict:
        """Single-turn prompt. Returns {response, input_tokens, output_tokens, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{GEMINI_BASE_URL}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "response": text,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "_trust": "untrusted_external",
            }
        except Exception as exc:
            logger.warning("GeminiAdapter.generate error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def chat(self, messages: list[dict], model: str = DEFAULT_MODEL) -> dict:
        """Multi-turn chat. Returns {response, input_tokens, output_tokens, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {"model": model, "messages": messages}
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{GEMINI_BASE_URL}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "response": text,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "_trust": "untrusted_external",
            }
        except Exception as exc:
            logger.warning("GeminiAdapter.chat error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def health_check(self) -> dict:
        try:
            result = await self.generate("Say 'ok'", model=DEFAULT_MODEL, system="Reply with one word only.")
            return {"status": "ok", "response": result.get("response", ""), "model": DEFAULT_MODEL}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
