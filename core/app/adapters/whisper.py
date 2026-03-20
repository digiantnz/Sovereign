import os
import httpx

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "Systran/faster-whisper-medium")
# Note: OLLAMA_URL/eviction removed 2026-03-20 — whisper now on node04, no shared GPU

class WhisperAdapter:
    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav", language: str = None) -> dict:
        """
        Transcribe raw audio bytes via faster-whisper-server on node04.
        POSTs to the OpenAI-compatible /v1/audio/transcriptions endpoint.
        Returns the full JSON response including text, language, and segment detail.
        Update WHISPER_URL in secrets/whisper.env to point to node04 service URL.
        """
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
