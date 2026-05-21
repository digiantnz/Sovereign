"""OpenRouter adapter — multi-model LLM routing (OpenAI-compatible).

API: https://openrouter.ai/api/v1
Key: OPENROUTER_API_KEY in secrets/providers.env
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)

OR_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
TIMEOUT = 30.0


class OpenRouterAdapter:
    def __init__(self):
        self._api_key = os.environ.get("OPENROUTER_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://sovereign.digiantnz.com",
            "X-Title": "Sovereign",
        }

    async def generate(
        self,
        prompt: str,
        model: str = DEFAULT_MODEL,
        system: str = "You are a helpful assistant.",
    ) -> dict:
        """Single-turn prompt. Returns {response, input_tokens, output_tokens, model, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {
                "model": model,
                "route": "fallback",  # auto-route to any available free model if default is rate-limited
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{OR_BASE_URL}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "response": text,
                "model": data.get("model", model),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "_trust": "untrusted_external",
            }
        except Exception as exc:
            logger.warning("OpenRouterAdapter.generate error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def chat(self, messages: list[dict], model: str = DEFAULT_MODEL) -> dict:
        """Multi-turn chat. Returns {response, input_tokens, output_tokens, model, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {"model": model, "route": "fallback", "messages": messages}
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{OR_BASE_URL}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "response": text,
                "model": data.get("model", model),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "_trust": "untrusted_external",
            }
        except Exception as exc:
            logger.warning("OpenRouterAdapter.chat error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def list_models(self) -> dict:
        """Returns available models from OpenRouter."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{OR_BASE_URL}/models", headers=self._headers())
                r.raise_for_status()
                data = r.json()
            models = [m.get("id") for m in data.get("data", [])]
            return {"count": len(models), "models": models[:50]}
        except Exception as exc:
            logger.warning("OpenRouterAdapter.list_models error: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def health_check(self) -> dict:
        try:
            result = await self.generate("Say 'ok'", model=DEFAULT_MODEL, system="Reply with one word only.")
            return {"status": "ok", "response": result.get("response", ""), "model": result.get("model")}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
