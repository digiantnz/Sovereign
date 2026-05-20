import logging
import re
import httpx
from config import cfg as _cfg

_TIMEOUT  = 200.0  # qwen2.5:32b: 30-50s typical, headroom for complex passes
_THINK_RE = re.compile(r'<think>(.*?)</think>', re.DOTALL)
_log      = logging.getLogger(__name__)


def _extract_think(text: str) -> tuple:
    """Extract <think>...</think> blocks from text.
    Returns (clean_text, thinking_content) where thinking_content is the
    concatenated content of all think blocks (empty string if none)."""
    thoughts = []
    def _capture(m: re.Match) -> str:
        t = m.group(1).strip()
        if t:
            thoughts.append(t)
        return ""
    clean = _THINK_RE.sub(_capture, text).strip()
    return clean, "\n\n".join(thoughts)


def _strip_think(text: str) -> str:
    """Strip <think>...</think> blocks, logging each block at DEBUG."""
    def _replace(m: re.Match) -> str:
        thinking = m.group(1).strip()
        if thinking:
            _log.debug("llm_thinking: %s", thinking[:2000])
        return ""
    return _THINK_RE.sub(_replace, text).strip()


class OllamaAdapter:
    async def generate(self, prompt: str, model: str = None, fmt: str = None,
                       capture_thinking: bool = False) -> dict:
        payload = {"model": model or _cfg.models.primary_inference_model, "prompt": prompt, "stream": False}
        if fmt:
            payload["format"] = fmt
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post("http://ollama:11434/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
        raw = data.get("response", "")
        if capture_thinking:
            clean, thinking = _extract_think(raw)
            data["response"] = clean
            if thinking:
                data["thinking"] = thinking
        else:
            data["response"] = _strip_think(raw)
        return data

    async def running_models(self) -> list[dict]:
        """Query /api/ps — returns list of currently loaded models with VRAM usage."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("http://ollama:11434/api/ps")
                r.raise_for_status()
                return r.json().get("models", [])
        except Exception as exc:
            _log.warning("ollama running_models failed: %s", exc)
            return []

    async def list_local_models(self) -> list[dict]:
        """Query /api/tags — returns all locally installed models."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("http://ollama:11434/api/tags")
                r.raise_for_status()
                return r.json().get("models", [])
        except Exception as exc:
            _log.warning("ollama list_local_models failed: %s", exc)
            return []

    async def chat(self, messages: list[dict], model: str = None, fmt: str = None,
                   capture_thinking: bool = False) -> dict:
        """Call /api/chat with a messages array (system/user/assistant roles).
        Returns a dict with a 'response' key containing the assistant's reply text,
        matching the shape of generate() for a consistent interface."""
        payload = {"model": model or _cfg.models.primary_inference_model, "messages": messages, "stream": False}
        if fmt:
            payload["format"] = fmt
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post("http://ollama:11434/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        raw_content = data.get("message", {}).get("content", "")
        if capture_thinking:
            content, thinking = _extract_think(raw_content)
            result = {"response": content, "model": data.get("model", model)}
            if thinking:
                result["thinking"] = thinking
            return result
        content = _strip_think(raw_content)
        return {"response": content, "model": data.get("model", model)}
