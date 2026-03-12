"""Claude API adapter (Anthropic — Messages API via httpx).

External cognition — must only be called via CognitionEngine.ask_claude(),
which ensures DCL classification and audit logging.
"""

import os
import httpx

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION  = "2023-06-01"
DEFAULT_MODEL      = "claude-sonnet-4-6"
TIMEOUT            = 90.0
MAX_TOKENS         = 2048


class ClaudeAdapter:
    def __init__(self):
        self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "x-api-key":         self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type":      "application/json",
        }

    async def generate(
        self,
        prompt:     str,
        model:      str = DEFAULT_MODEL,
        system:     str = "You are a helpful assistant.",
        max_tokens: int = MAX_TOKENS,
    ) -> dict:
        """Single-turn prompt. Returns {response, input_tokens, output_tokens}."""
        payload = {
            "model":      model,
            "max_tokens": max_tokens,
            "system":     system,
            "messages":   [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{ANTHROPIC_BASE_URL}/messages",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        text  = data["content"][0]["text"]
        usage = data.get("usage", {})
        return {
            "response":      text,
            "input_tokens":  usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }

    async def chat(
        self,
        messages:   list[dict],
        model:      str = DEFAULT_MODEL,
        system:     str = "You are a helpful assistant.",
        max_tokens: int = MAX_TOKENS,
    ) -> dict:
        """Multi-turn chat. Messages: [{role, content}, ...]. Returns same schema."""
        payload = {
            "model":      model,
            "max_tokens": max_tokens,
            "system":     system,
            "messages":   messages,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{ANTHROPIC_BASE_URL}/messages",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        text  = data["content"][0]["text"]
        usage = data.get("usage", {})
        return {
            "response":      text,
            "input_tokens":  usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
