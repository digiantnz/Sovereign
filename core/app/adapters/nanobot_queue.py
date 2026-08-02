"""Sovereign Nanobot Dispatch Queue

Bounded-concurrency asyncio priority queue, one instance per nanobot skill
name. Modeled on adapters/inference_queue.py's InferenceQueue (same
dataclass/_worker_loop/_run_one shape, same priority levels, same
diagnostic-logging-on-timeout pattern) but adapted on three points:

  1. N-worker pool, not single-worker. Ollama/GPU is one exclusive resource
     (hence InferenceQueue's single worker); nanobot-01 forwards to backends
     (a2a-browser, IMAP, WebDAV) that can genuinely serve several requests
     concurrently.

  2. One queue per skill name, not one global queue. sovereign-browser (→
     a2a-browser, demonstrated contention), nc-mail (→ IMAP), and
     openclaw-nextcloud/sovereign-nextcloud-fs (→ Nextcloud WebDAV) hit
     unrelated backends with unrelated real capacities. A single global cap
     would throttle a fast interactive mail check behind an unrelated
     background browser-search burst.

  3. Timeout propagates as a RAISED asyncio.TimeoutError through the
     awaited future (future.set_exception), not resolved as a status dict.
     This lets adapters/nanobot.py's _forward() catch it with one new
     except clause alongside its existing except httpx.TimeoutException,
     reusing its established error-dict convention instead of inventing a
     new one. Contrast InferenceQueue, which deliberately resolves timeouts
     via set_result() because ITS callers (e.g. cog.ask_local()) expect a
     dict, never an exception — different callers, different (both
     correct) convention.

Concurrency defaults below are a reasoned starting point from ONE observed
production burst (18 simultaneous sovereign-browser calls; the 4 slowest
returned empty after ~65s each) — not a benchmarked capacity ceiling for
a2a-browser, IMAP, or WebDAV. Tune down further (e.g. 2-3) if empty-content
results persist, rather than reintroducing an ad-hoc counter.
"""

import asyncio
import logging
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Same priority scheme as InferenceQueue, for consistency — but per the
# stated non-goal, every current call site uses NORMAL. HIGH/LOW are wired
# for future use, not threaded through any call site yet. InferenceQueue's
# HIGH-priority grace period / pre-emption-peek logic is deliberately NOT
# ported here — that solved a GPU/Director-interactive-specific problem;
# lift it later from inference_queue.py if priority threading is ever added.
HIGH   = 1
NORMAL = 2
LOW    = 3
_LABELS = {1: "HIGH", 2: "NORMAL", 3: "LOW"}

DEFAULT_CONCURRENCY = 6   # generous fallback for skills with no evidence either way

# Per-skill overrides. sovereign-browser is the only skill with observed
# contention data (18-deep burst, 4 empty results at ~65s).
SKILL_CONCURRENCY_OVERRIDES: dict[str, int] = {
    "sovereign-browser": 4,
}


def concurrency_for_skill(skill: str) -> int:
    return SKILL_CONCURRENCY_OVERRIDES.get(skill, DEFAULT_CONCURRENCY)


@dataclass(order=True)
class _NanobotJob:
    # order=True sorts priority first, then seq — mirrors InferenceQueue's
    # _InferenceJob exactly (FIFO within a priority level).
    priority:     int
    seq:          int
    job_id:       str                          = field(compare=False)
    coro_fn:      Callable[[], Awaitable[Any]]  = field(compare=False)
    timeout:      float                         = field(compare=False)
    future:       "asyncio.Future"              = field(compare=False)
    submitted_at: float                         = field(compare=False)


class NanobotQueue:
    """Bounded-concurrency priority queue for one nanobot skill.

    coro_fn passed to submit() is a zero-arg async thunk — this class has
    no knowledge of HTTP/nanobot specifics, keeping it a dependency-free,
    reusable primitive (unlike InferenceQueue, which is Ollama-specific).
    """

    HIGH, NORMAL, LOW = HIGH, NORMAL, LOW
    _LABELS = _LABELS

    def __init__(self, skill: str, concurrency: int, ledger=None) -> None:
        self.skill        = skill
        self.concurrency  = concurrency
        self._ledger      = ledger
        self._queue: "asyncio.PriorityQueue" = asyncio.PriorityQueue()
        self._seq         = 0
        self._active      = 0   # workers currently executing (not idle on .get())
        self._worker_tasks: list[asyncio.Task] = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit(self, coro_fn: Callable[[], Awaitable[Any]],
                      timeout: float, priority: int = NORMAL) -> Any:
        """Submit a zero-arg async thunk; await and return its result.

        Timeout is wall-clock from DEQUEUE, not submission — a job queued
        behind a burst of siblings is not itself "slow" (mirrors
        InferenceQueue's documented behavior exactly).

        Raises asyncio.TimeoutError if the job doesn't complete within
        `timeout` of being dequeued. Any other exception raised by coro_fn()
        propagates unchanged.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._seq += 1
        job = _NanobotJob(
            priority=priority, seq=self._seq,
            job_id=str(_uuid.uuid4())[:8],
            coro_fn=coro_fn, timeout=timeout, future=future,
            submitted_at=time.monotonic(),
        )
        await self._queue.put(job)
        return await future   # re-raises whatever the worker set_exception'd

    # ── Observability ─────────────────────────────────────────────────────────

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def active_count(self) -> int:
        """Workers currently executing a job (0..concurrency)."""
        return self._active

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(i), name=f"nanobot_queue_{self.skill}_{i}")
            for i in range(self.concurrency)
        ]
        logger.info("NanobotQueue[%s]: %d workers started", self.skill, self.concurrency)

    async def stop(self) -> None:
        """Drain pending jobs (each rejected with TimeoutError) then cancel workers."""
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
                if not job.future.done():
                    job.future.set_exception(
                        asyncio.TimeoutError(f"NanobotQueue[{self.skill}] stopped before job ran")
                    )
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        for t in self._worker_tasks:
            t.cancel()
        for t in self._worker_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        logger.info("NanobotQueue[%s]: workers stopped", self.skill)

    # ── Workers ───────────────────────────────────────────────────────────────

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            try:
                await self._run_one(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("NanobotQueue[%s] worker %d: crashed — restarting: %s",
                             self.skill, worker_id, exc)
                await asyncio.sleep(0.1)

    async def _run_one(self, worker_id: int) -> None:
        job: _NanobotJob = await self._queue.get()
        wait_s = round(time.monotonic() - job.submitted_at, 2)
        self._active += 1
        try:
            result = await asyncio.wait_for(job.coro_fn(), timeout=job.timeout)
            if not job.future.done():
                job.future.set_result(result)
        except asyncio.TimeoutError:
            depth  = self.queue_depth()
            active = self._active
            logger.warning(
                "NanobotQueue[%s]: job=%s timed out after %.0fs — waited=%.2fs in "
                "queue before dispatch, queue_depth=%d active_workers=%d/%d",
                self.skill, job.job_id, job.timeout, wait_s, depth, active, self.concurrency,
            )
            if self._ledger:
                try:
                    self._ledger.append(
                        "nanobot_queue_timeout", "nanobot_queue",
                        {"skill": self.skill, "job_id": job.job_id, "timeout_s": job.timeout,
                         "waited_s": wait_s, "queue_depth_at_timeout": depth,
                         "active_workers": active, "concurrency": self.concurrency},
                    )
                except Exception:
                    pass
            if not job.future.done():
                job.future.set_exception(asyncio.TimeoutError(
                    f"NanobotQueue[{self.skill}]: timed out after {job.timeout}s "
                    f"(waited {wait_s}s in queue; {active}/{self.concurrency} workers active)"
                ))
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.cancel()
            raise
        except Exception as exc:
            # Any other exception from coro_fn() (httpx.ConnectError,
            # httpx.TimeoutException, etc.) propagates through the future
            # unchanged — _forward()'s existing except clauses handle it.
            if not job.future.done():
                job.future.set_exception(exc)
        finally:
            self._active -= 1
            self._queue.task_done()
