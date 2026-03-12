import httpx

MODEL = "mistral:7b-instruct-q4_K_M"


class OllamaAdapter:
    async def generate(self, prompt: str, model: str = MODEL, fmt: str = None) -> dict:
        payload = {"model": model, "prompt": prompt, "stream": False}
        if fmt:
            payload["format"] = fmt
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post("http://ollama:11434/api/generate", json=payload)
            r.raise_for_status()
            return r.json()

    async def chat(self, messages: list[dict], model: str = MODEL, fmt: str = None) -> dict:
        """Call /api/chat with a messages array (system/user/assistant roles).
        Returns a dict with a 'response' key containing the assistant's reply text,
        matching the shape of generate() for a consistent interface."""
        payload = {"model": model, "messages": messages, "stream": False}
        if fmt:
            payload["format"] = fmt
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post("http://ollama:11434/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        # Normalise: extract content from message.content → response key
        content = data.get("message", {}).get("content", "")
        return {"response": content, "model": data.get("model", model)}
