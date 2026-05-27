"""Sovereign LLM Inference Queue

Single-worker asyncio queue that serialises all Ollama synthesis calls.
Prevents GPU contention between the cognitive loop and background harnesses.

Priority model (lower number = higher priority):
  HIGH   = 1  — gateway/Telegram interactive messages (cognitive loop passes)
  NORMAL = 2  — all other LLM work (harnesses, research, portfolio, learning, SI)
  LOW    = 3  — perpetual memory synthesis ONLY

Grace period: after any HIGH job completes, non-HIGH jobs are held for
_HIGH_GRACE_S seconds (240). This keeps the GPU free for Director follow-up
messages. If another HIGH job arrives during the grace window it runs
immediately; once the window expires non-HIGH jobs proceed normally.

Pre-emption check: before a NORMAL/LOW job starts executing, the worker
peeks at the queue. If a HIGH job is already waiting, the NORMAL/LOW job
re-queues itself so the HIGH job can run first. This prevents an in-flight
NORMAL job from blocking a Director message that arrives mid-generation.

On timeout, the worker resolves the future with a structured dict rather than
raising — callers check result.get("status") == "llm_timeout" and may resubmit
the original call as a fresh queue entry. No retry memory lives in the queue.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

from adapters.ollama import OllamaAdapter

logger = logging.getLogger(__name__)

# Brief pause after cancelling a timed-out job. Gives Ollama time to notice
# the aborted HTTP connection before the next job starts.
_OLLAMA_RESET_DELAY_S = 1.0

# After a HIGH job completes, non-HIGH jobs wait this many seconds so the
# Director can send follow-up messages without hitting GPU contention.
_HIGH_GRACE_S = 240.0
# Poll interval while waiting out the grace period.
_HIGH_GRACE_POLL_S = 10.0
# Actual-wait threshold: only show "GPU was busy" banner when wait > this.
_QUEUE_WAIT_BANNER_THRESHOLD_S = 3.0


@dataclass(order=True)
class _InferenceJob:
    # order=True sorts on fields in declaration order — priority first, then seq.
    # seq is a monotonic counter that guarantees FIFO within the same priority level.
    priority:     int
    seq:          int
    job_id:       str          = field(compare=False)
    call_type:    str          = field(compare=False)  # "generate" | "chat"
    kwargs:       dict         = field(compare=False)
    timeout:      float        = field(compare=False)
    future:       asyncio.Future = field(compare=False)
    submitted_at: float        = field(compare=False)


class InferenceQueue:
    HIGH   = 1
    NORMAL = 2
    LOW    = 3

    _LABELS = {1: "HIGH", 2: "NORMAL", 3: "LOW"}

    def __init__(self, ollama: OllamaAdapter, ledger=None) -> None:
        self._ollama        = ollama
        self._ledger        = ledger
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq           = 0
        self._busy          = False
        self._current_job: dict | None = None
        self._worker_task: asyncio.Task | None = None
        self._last_high_ts: float = 0.0  # monotonic time of last HIGH job completion

    # ── Public API ────────────────────────────────────────────────────────────

    async def generate(
        self,
        prompt:           str,
        model:            str | None  = None,
        fmt:              str | None  = None,
        priority:         int         = NORMAL,
        timeout:          float       = 200.0,
        capture_thinking: bool        = False,
    ) -> dict:
        """Submit a generate job and await the result.

        Returns the Ollama response dict on success, or a structured timeout
        dict when the job's timeout expires (status == "llm_timeout").
        """
        import uuid as _uuid
        loop    = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._seq += 1
        waited_behind_lower = (
            self._busy
            and self._current_job is not None
            and self._current_job.get("priority", 99) > priority
        )
        job = _InferenceJob(
            priority=priority, seq=self._seq,
            job_id=str(_uuid.uuid4())[:8],
            call_type="generate",
            kwargs={"prompt": prompt, "model": model, "fmt": fmt,
                    "capture_thinking": capture_thinking},
            timeout=timeout,
            future=future,
            submitted_at=time.monotonic(),
        )
        await self._queue.put(job)
        result = await future
        if isinstance(result, dict):
            result["_queue_waited"] = waited_behind_lower
        return result

    async def chat(
        self,
        messages: list[dict],
        model:    str | None  = None,
        fmt:      str | None  = None,
        priority: int         = NORMAL,
        timeout:  float       = 200.0,
    ) -> dict:
        """Submit a chat job and await the result. Same contract as generate()."""
        import uuid as _uuid
        loop    = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._seq += 1
        waited_behind_lower = (
            self._busy
            and self._current_job is not None
            and self._current_job.get("priority", 99) > priority
        )
        job = _InferenceJob(
            priority=priority, seq=self._seq,
            job_id=str(_uuid.uuid4())[:8],
            call_type="chat",
            kwargs={"messages": messages, "model": model, "fmt": fmt},
            timeout=timeout,
            future=future,
            submitted_at=time.monotonic(),
        )
        await self._queue.put(job)
        result = await future
        if isinstance(result, dict):
            result["_queue_waited"] = waited_behind_lower
        return result

    # ── Observability ─────────────────────────────────────────────────────────

    def queue_depth(self) -> int:
        """Number of pending jobs (not counting the in-flight job)."""
        return self._queue.qsize()

    def is_busy(self) -> bool:
        """True while the worker is executing an inference call."""
        return self._busy

    def current_job(self) -> dict | None:
        """Metadata for the in-flight job, or None if idle."""
        return self._current_job

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the single worker coroutine. Call once in app lifespan startup."""
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="inference_queue_worker"
        )
        logger.info("InferenceQueue: worker started")

    async def stop(self) -> None:
        """Drain pending jobs (resolving each with a timeout result) then stop."""
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
                if not job.future.done():
                    job.future.set_result(self._timeout_result(job))
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("InferenceQueue: worker stopped")

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker_loop(self) -> None:
        """Single worker — runs forever, restarting on unexpected errors."""
        while True:
            try:
                await self._run_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("InferenceQueue: worker error — restarting: %s", exc)
                if self._ledger:
                    self._ledger.append(
                        "inference_queue_worker_crash", "inference_queue",
                        {"error": str(exc)},
                    )
                self._busy = False
                self._current_job = None
                await asyncio.sleep(0.1)

    async def _run_one(self) -> None:
        job: _InferenceJob = await self._queue.get()

        # Grace period: hold non-HIGH jobs for up to _HIGH_GRACE_S after a HIGH job
        # so the Director can send follow-up messages without GPU contention.
        if job.priority > self.HIGH and self._last_high_ts > 0:
            elapsed = time.monotonic() - self._last_high_ts
            remaining = _HIGH_GRACE_S - elapsed
            if remaining > 0:
                logger.debug(
                    "InferenceQueue: grace hold — job=%s priority=%s remaining=%.0fs",
                    job.job_id, self._LABELS.get(job.priority, str(job.priority)), remaining,
                )
                # Re-queue the job and yield so arriving HIGH jobs can jump ahead.
                await self._queue.put(job)
                self._queue.task_done()
                await asyncio.sleep(min(_HIGH_GRACE_POLL_S, remaining))
                return

        # Pre-emption check: if a HIGH job is already queued, re-queue this NORMAL/LOW
        # job and yield — HIGH jobs must never wait behind an in-flight lower-priority job.
        if job.priority > self.HIGH and self._queue.qsize() > 0:
            queued = list(self._queue._queue)  # snapshot of heapq internals
            if any(j.priority == self.HIGH for j in queued):
                logger.debug(
                    "InferenceQueue: HIGH job queued — deferring %s job=%s",
                    self._LABELS.get(job.priority, str(job.priority)), job.job_id,
                )
                await self._queue.put(job)
                self._queue.task_done()
                await asyncio.sleep(0.1)
                return

        self._busy = True
        self._current_job = {
            "priority":       job.priority,
            "priority_label": self._LABELS.get(job.priority, str(job.priority)),
            "submitted_at":   job.submitted_at,
            "call_type":      job.call_type,
            "job_id":         job.job_id,
        }
        wait_s = round(time.monotonic() - job.submitted_at, 2)
        logger.debug(
            "InferenceQueue: executing job=%s priority=%s waited=%.2fs",
            job.job_id, self._LABELS.get(job.priority, str(job.priority)), wait_s,
        )
        try:
            result = await asyncio.wait_for(
                self._dispatch(job), timeout=job.timeout,
            )
            if isinstance(result, dict):
                result["_queue_wait_seconds"] = wait_s
            if not job.future.done():
                job.future.set_result(result)
        except asyncio.TimeoutError:
            logger.warning(
                "InferenceQueue: job=%s timed out after %.0fs (priority=%s)",
                job.job_id, job.timeout,
                self._LABELS.get(job.priority, str(job.priority)),
            )
            if self._ledger:
                self._ledger.append(
                    "inference_timeout", "inference_queue",
                    {"job_id": job.job_id, "priority": job.priority,
                     "timeout_s": job.timeout, "waited_s": wait_s},
                )
            if not job.future.done():
                job.future.set_result(self._timeout_result(job))
            # Give Ollama a moment to notice the aborted connection
            await asyncio.sleep(_OLLAMA_RESET_DELAY_S)
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.cancel()
            raise
        except Exception as exc:
            logger.error("InferenceQueue: job=%s failed: %s", job.job_id, exc)
            if not job.future.done():
                job.future.set_exception(exc)
        finally:
            self._busy = False
            if job.priority == self.HIGH:
                self._last_high_ts = time.monotonic()
            self._current_job = None
            self._queue.task_done()

    async def _dispatch(self, job: _InferenceJob) -> dict:
        kw = job.kwargs
        if job.call_type == "generate":
            return await self._ollama.generate(
                prompt=kw["prompt"],
                model=kw.get("model"),
                fmt=kw.get("fmt"),
                capture_thinking=kw.get("capture_thinking", False),
            )
        return await self._ollama.chat(
            messages=kw["messages"],
            model=kw.get("model"),
            fmt=kw.get("fmt"),
            capture_thinking=kw.get("capture_thinking", False),
        )

    def _timeout_result(self, job: _InferenceJob) -> dict:
        raw = job.kwargs.get("prompt") or str(job.kwargs.get("messages", ""))
        return {
            "status":               "llm_timeout",
            "priority":             self._LABELS.get(job.priority, str(job.priority)),
            "timeout_seconds":      job.timeout,
            "retryable":            True,
            "original_prompt_hash": hashlib.sha256(raw.encode()).hexdigest()[:16],
            "response":             "",
        }
