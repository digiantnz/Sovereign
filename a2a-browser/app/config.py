import os

# Auth
SHARED_SECRET = os.environ.get("A2A_SHARED_SECRET", "")

# Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral:7b-instruct-q4_K_M")

# Search backends
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")   # e.g. http://searxng:8080
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BING_API_KEY = os.environ.get("BING_API_KEY", "")

# Rate limiting (requests per minute per backend)
RATE_LIMIT = int(os.environ.get("A2A_RATE_LIMIT", "10"))

# Max search results per backend call
MAX_RESULTS = int(os.environ.get("A2A_MAX_RESULTS", "10"))

# Snippet max length (chars) in sanitised output
MAX_SNIPPET_LEN = 500
MAX_TITLE_LEN = 200

# Backends available (determined at startup based on configured keys)
def enabled_backends() -> list[str]:
    backends = []
    if SEARXNG_URL:
        backends.append("searxng")  # primary — multi-engine aggregation
    backends.append("ddg")          # always available as fallback
    if BRAVE_API_KEY:
        backends.append("brave")
    if BING_API_KEY:
        backends.append("bing")
    return backends
