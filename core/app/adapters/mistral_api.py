"""Mistral API adapter stub — not yet wired.

Enable by setting MISTRAL_API_KEY in secrets/providers.env and
requesting Director approval to enable in provider_registry.
"""

import os


class MistralAPIAdapter:
    def __init__(self):
        self._api_key = os.environ.get("MISTRAL_API_KEY", "")

    async def health_check(self) -> dict:
        return {"status": "stub", "enabled": False, "_trust": "untrusted_external"}

    async def generate(self, prompt: str, model: str = "mistral-large-latest") -> dict:
        raise NotImplementedError("MistralAPIAdapter not yet enabled — awaiting Director approval")

    async def chat(self, messages: list[dict], model: str = "mistral-large-latest") -> dict:
        raise NotImplementedError("MistralAPIAdapter not yet enabled — awaiting Director approval")
