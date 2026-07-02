"""Grok API adapter (xAI — Responses API).

External cognition — must only be called via CognitionEngine.ask_grok(),
which ensures DCL classification and audit logging.

Uses POST /v1/responses (not the legacy /v1/chat/completions) for both plain
and search-augmented calls — xAI deprecated the old chat-completions
search_parameters field (Live Search) in favour of the Agent Tools API's
`web_search` tool, and the Responses API is the uniform surface for both.
"""

import os
import httpx

GROK_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL  = "grok-4.3"
TIMEOUT        = 60.0


def _extract_text(data: dict) -> str:
    """Pull the assistant's final text from a Responses API payload.

    `output` is a flat list of typed items (reasoning, web_search_call,
    message, ...) — the answer lives in the last item with type=="message".
    """
    for item in reversed(data.get("output", [])):
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    return block.get("text", "")
    return ""


class GrokAdapter:
    def __init__(self):
        self._api_key = os.environ.get("GROK_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }

    async def _post(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{GROK_BASE_URL}/responses",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage", {})
        return {
            "response":      _extract_text(data),
            "input_tokens":  usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }

    async def generate(
        self,
        prompt:  str,
        model:   str = DEFAULT_MODEL,
        system:  str = "You are a helpful assistant.",
        search:  bool = False,
    ) -> dict:
        """Send a single-turn prompt. Returns {response, input_tokens, output_tokens}.

        search=True enables Grok's web_search tool (real-time data) — required for
        task_types that expect current-events/web-aware answers, otherwise Grok
        silently answers from training data only.
        """
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
        }
        if search:
            payload["tools"] = [{"type": "web_search"}]
        return await self._post(payload)

    async def chat(self, messages: list[dict], model: str = DEFAULT_MODEL) -> dict:
        """Multi-turn chat. Messages: [{role, content}, ...]. Returns same schema."""
        return await self._post({"model": model, "input": messages})
