"""Ollama Cloud adapter — native Ollama API (NOT OpenAI-compatible).

Base URL: https://ollama.com
Auth: Authorization: Bearer {OLLAMA_CLOUD_API_KEY}
API format mirrors local Ollama: /api/chat, /api/generate, /api/tags
Key: OLLAMA_CLOUD_API_KEY in secrets/providers.env
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)

OLLAMA_CLOUD_BASE_URL = "https://ollama.com"
DEFAULT_MODEL = "gemma3:4b"
TIMEOUT = 30.0


class OllamaCloudAdapter:
    def __init__(self):
        self._api_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")

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
        """Single-turn generate. Returns {response, input_tokens, output_tokens, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {
                "model": model,
                "prompt": prompt,
                "system": system,
                "stream": False,
            }
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{OLLAMA_CLOUD_BASE_URL}/api/generate",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            return {
                "response": data.get("response", ""),
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "_trust": "untrusted_external",
            }
        except Exception as exc:
            logger.warning("OllamaCloudAdapter.generate error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def chat(self, messages: list[dict], model: str = DEFAULT_MODEL) -> dict:
        """Multi-turn chat. Returns {response, input_tokens, output_tokens, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {"model": model, "messages": messages, "stream": False}
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{OLLAMA_CLOUD_BASE_URL}/api/chat",
                    headers=self._headers(),
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            text = data.get("message", {}).get("content", "")
            return {
                "response": text,
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "_trust": "untrusted_external",
            }
        except Exception as exc:
            logger.warning("OllamaCloudAdapter.chat error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def list_models(self) -> dict:
        """Returns available models from Ollama Cloud."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    f"{OLLAMA_CLOUD_BASE_URL}/api/tags",
                    headers=self._headers(),
                )
                r.raise_for_status()
                data = r.json()
            models = [m.get("name") for m in data.get("models", [])]
            return {"count": len(models), "models": models}
        except Exception as exc:
            logger.warning("OllamaCloudAdapter.list_models error: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def health_check(self) -> dict:
        try:
            result = await self.chat(
                [{"role": "user", "content": "Say 'ok' and nothing else."}],
                model=DEFAULT_MODEL,
            )
            return {"status": "ok", "response": result.get("response", ""), "model": DEFAULT_MODEL}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
