"""Grok API adapter (xAI — OpenAI-compatible endpoint).

External cognition — must only be called via CognitionEngine.ask_grok(),
which ensures DCL classification and audit logging.
"""

import os
import httpx

GROK_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL  = "grok-3"
TIMEOUT        = 60.0


class GrokAdapter:
    def __init__(self):
        self._api_key = os.environ.get("GROK_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }

    async def generate(
        self,
        prompt:  str,
        model:   str = DEFAULT_MODEL,
        system:  str = "You are a helpful assistant.",
    ) -> dict:
        """Send a single-turn prompt. Returns {response, input_tokens, output_tokens}."""
        payload = {
            "model":    model,
            "messages": [
                {"role": "system",  "content": system},
                {"role": "user",    "content": prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{GROK_BASE_URL}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data   = r.json()
        text  = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "response":      text,
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

    async def chat(self, messages: list[dict], model: str = DEFAULT_MODEL) -> dict:
        """Multi-turn chat. Messages: [{role, content}, ...]. Returns same schema."""
        payload = {"model": model, "messages": messages}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{GROK_BASE_URL}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data   = r.json()
        text  = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "response":      text,
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
