"""Groq Inference adapter — fast cloud LLM inference (OpenAI-compatible).

API: https://api.groq.com/openai/v1
Key: GROQ_API_KEY in secrets/providers.env

Two call paths:
  generate() / chat()       — freeform text via raw httpx (used by PASS 2/3a routing)
  generate_structured()     — Instructor-decorated structured JSON output
                              requires: pip install groq instructor
                              falls back gracefully if packages absent (logs WARNING, returns None)
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)

GROQ_BASE_URL    = "https://api.groq.com/openai/v1"
DEFAULT_MODEL    = "llama-3.3-70b-versatile"
REASONING_MODEL  = "deepseek-r1-distill-llama-70b"   # preferred for structured/heavy reasoning
TIMEOUT          = 30.0


class GroqInferenceAdapter:
    def __init__(self):
        self._api_key = os.environ.get("GROQ_API_KEY", "")

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
                    f"{GROQ_BASE_URL}/chat/completions",
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
            logger.warning("GroqInferenceAdapter.generate error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def chat(self, messages: list[dict], model: str = DEFAULT_MODEL) -> dict:
        """Multi-turn chat. Returns {response, input_tokens, output_tokens, _trust}."""
        if not self._api_key:
            return {"status": "error", "error": "API_KEY not configured", "_trust": "untrusted_external"}
        try:
            payload = {"model": model, "messages": messages}
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{GROQ_BASE_URL}/chat/completions",
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
            logger.warning("GroqInferenceAdapter.chat error: %s", exc)
            return {"status": "error", "error": str(exc), "_trust": "untrusted_external"}

    async def generate_structured(
        self,
        prompt: str,
        response_model: type,
        model: str | None = None,
        system: str = "You are a helpful assistant. Return only valid JSON.",
        max_retries: int = 2,
    ):
        """Instructor-decorated structured output via Groq.

        Uses deepseek-r1-distill-llama-70b by default (reasoning model).
        Returns a validated Pydantic model instance, or None if groq/instructor
        packages are unavailable or the call fails after retries.
        """
        if not self._api_key:
            logger.warning("generate_structured: GROQ_API_KEY not configured")
            return None
        try:
            from groq import AsyncGroq
            import instructor
        except ImportError:
            logger.warning(
                "generate_structured: groq/instructor packages not installed — "
                "add 'groq' and 'instructor[groq]' to requirements.txt and rebuild"
            )
            return None
        try:
            client = instructor.from_groq(
                AsyncGroq(api_key=self._api_key),
                mode=instructor.Mode.GROQ_TOOLS,
            )
            chosen_model = model or REASONING_MODEL
            result = await client.chat.completions.create(
                model=chosen_model,
                response_model=response_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                max_retries=max_retries,
            )
            return result
        except Exception as exc:
            logger.warning("generate_structured error (model=%s): %s", model or REASONING_MODEL, exc)
            return None

    async def health_check(self) -> dict:
        try:
            result = await self.generate("Say 'ok'", model=DEFAULT_MODEL, system="Reply with one word only.")
            return {"status": "ok", "response": result.get("response", ""), "model": DEFAULT_MODEL}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
