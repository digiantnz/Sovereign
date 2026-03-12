import os
import httpx

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "Systran/faster-whisper-medium")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "mistral:7b-instruct-q4_K_M")

class WhisperAdapter:
    async def _evict_ollama(self):
        """
        Unload the Ollama model from VRAM before transcription.

        mistral:7b-instruct-q4_K_M (~4.4 GB) + faster-whisper-medium (~769 MB)
        fit within the 3060 Ti's 7.6 GB available, but evicting Ollama first
        avoids fragmentation and guarantees clean VRAM for the Whisper runtime.
        Ollama will lazy-reload on the next /query request.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": OLLAMA_MODEL, "keep_alive": 0},
                )
            except Exception:
                pass  # Non-fatal — best-effort eviction

    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav", language: str = None) -> dict:
        """
        Transcribe raw audio bytes via faster-whisper-server.

        Evicts Ollama from VRAM first, then POSTs to the OpenAI-compatible
        /v1/audio/transcriptions endpoint. Returns the full JSON response
        including text, language, and segment-level detail.
        """
        await self._evict_ollama()

        files = {"file": (filename, audio_bytes, "audio/wav")}
        data = {"model": WHISPER_MODEL, "response_format": "verbose_json"}
        if language:
            data["language"] = language

        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{WHISPER_URL}/v1/audio/transcriptions",
                files=files,
                data=data,
            )
            r.raise_for_status()
            return r.json()
