import asyncio
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class BackendStats:
    count: int = 0
    successes: int = 0
    total_latency_ms: float = 0.0

    def success_rate(self) -> float:
        return round(self.successes / self.count, 3) if self.count else 0.0

    def avg_latency(self) -> float:
        return round(self.total_latency_ms / self.count, 1) if self.count else 0.0


class MetricsStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._total = 0
        self._successes = 0
        self._total_latency_ms = 0.0
        self._backends: Dict[str, BackendStats] = {}

    async def record(self, backend: str, success: bool, latency_ms: float):
        async with self._lock:
            self._total += 1
            self._total_latency_ms += latency_ms
            if success:
                self._successes += 1
            if backend not in self._backends:
                self._backends[backend] = BackendStats()
            s = self._backends[backend]
            s.count += 1
            s.total_latency_ms += latency_ms
            if success:
                s.successes += 1

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "request_count": self._total,
                "success_rate": round(self._successes / self._total, 3) if self._total else 0.0,
                "avg_latency_ms": round(self._total_latency_ms / self._total, 1) if self._total else 0.0,
                "backend_distribution": {
                    name: {
                        "count": s.count,
                        "success_rate": s.success_rate(),
                        "avg_latency_ms": s.avg_latency(),
                    }
                    for name, s in self._backends.items()
                },
            }


# Module-level singleton — initialised in lifespan
_store: MetricsStore = None


def init() -> MetricsStore:
    global _store
    _store = MetricsStore()
    return _store


def get() -> MetricsStore:
    return _store
