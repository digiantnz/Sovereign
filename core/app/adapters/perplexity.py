"""Perplexity adapter stub — not yet wired.

Enable by setting PERPLEXITY_API_KEY in secrets/providers.env and
requesting Director approval to enable in provider_registry.
"""

import os


class PerplexityAdapter:
    def __init__(self):
        self._api_key = os.environ.get("PERPLEXITY_API_KEY", "")

    async def health_check(self) -> dict:
        return {"status": "stub", "enabled": False, "_trust": "untrusted_external"}

    async def generate(self, prompt: str, model: str = "llama-3.1-sonar-large-128k-online") -> dict:
        raise NotImplementedError("PerplexityAdapter not yet enabled — awaiting Director approval")

    async def chat(self, messages: list[dict], model: str = "llama-3.1-sonar-large-128k-online") -> dict:
        raise NotImplementedError("PerplexityAdapter not yet enabled — awaiting Director approval")
