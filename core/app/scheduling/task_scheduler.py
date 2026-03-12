"""Generalised task scheduler — data-driven, no task-specific code.

Tasks live in three Qdrant collections:
  PROSPECTIVE  — scheduling metadata (when to run, status, next_due)
  PROCEDURAL   — execution steps (how to run; human_confirmed required)
  EPISODIC     — run history (what happened, timestamped)

All three entries share a `task_id` UUID for coherent lookup.

The scheduler loop runs every 60 seconds. Any PROSPECTIVE entry with
  status="active" and next_due <= now(UTC)
is executed. Steps are dispatched via the injected _dispatch_fn so the
full adapter pipeline (governance already cleared at schedule-time) runs.
Results are collected, Telegram-notified per notify_when policy, written
to episodic, and next_due advanced for recurring tasks.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta

import httpx

from execution.adapters.qdrant import PROSPECTIVE, PROCEDURAL, EPISODIC, WORKING

logger = logging.getLogger(__name__)

# ── Capability checks ─────────────────────────────────────────────────────────
# Each intent maps to a lambda that returns True if the adapter is reachable.
# These are best-effort env-var checks; the real error surfaces at run-time.

_CAPABILITY = {
    "fetch_email":    lambda: bool(os.environ.get("PERSONAL_IMAP_HOST") or os.environ.get("BUSINESS_IMAP_HOST")),
    "search_email":   lambda: bool(os.environ.get("PERSONAL_IMAP_HOST") or os.environ.get("BUSINESS_IMAP_HOST")),
    "move_email":     lambda: bool(os.environ.get("PERSONAL_IMAP_HOST") or os.environ.get("BUSINESS_IMAP_HOST")),
    "send_email":     lambda: bool(os.environ.get("PERSONAL_SMTP_HOST") or os.environ.get("BUSINESS_SMTP_HOST")),
    "list_files":     lambda: bool(os.environ.get("NEXTCLOUD_URL", "http://nextcloud")),
    "read_file":      lambda: bool(os.environ.get("NEXTCLOUD_URL", "http://nextcloud")),
    "write_file":     lambda: bool(os.environ.get("NEXTCLOUD_URL", "http://nextcloud")),
    "list_calendars": lambda: bool(os.environ.get("NEXTCLOUD_URL", "http://nextcloud")),
    "create_event":   lambda: bool(os.environ.get("NEXTCLOUD_URL", "http://nextcloud")),
    "create_task":    lambda: bool(os.environ.get("NEXTCLOUD_URL", "http://nextcloud")),
    "web_search":     lambda: bool(os.environ.get("A2A_BROWSER_URL", "http://172.16.201.4:8001")),
    "fetch_url":      lambda: bool(os.environ.get("A2A_BROWSER_URL", "http://172.16.201.4:8001")),
    "query":          lambda: True,   # Ollama always present
    "research":       lambda: True,
    "list_containers":lambda: True,   # broker always present
    "get_stats":      lambda: True,
}

_ADAPTER_NAMES = {
    "fetch_email": "IMAP", "search_email": "IMAP", "move_email": "IMAP",
    "send_email": "SMTP",
    "list_files": "WebDAV", "read_file": "WebDAV", "write_file": "WebDAV",
    "list_calendars": "CalDAV", "create_event": "CalDAV", "create_task": "CalDAV",
    "web_search": "Browser", "fetch_url": "Browser",
    "query": "Ollama", "research": "Ollama",
    "list_containers": "Broker", "get_stats": "Broker",
}


# ── Cron helpers ──────────────────────────────────────────────────────────────

def _field_matches(value: int, field: str) -> bool:
    """Return True if `value` matches the cron field expression."""
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            start = 0 if base == "*" else int(base)
            if value >= start and (value - start) % step == 0:
                return True
        elif "-" in part:
            lo, hi = map(int, part.split("-", 1))
            if lo <= value <= hi:
                return True
        else:
            if value == int(part):
                return True
    return False


def cron_next(expr: str, after: datetime | None = None) -> datetime | None:
    """Return next datetime matching a 5-field cron expression (UTC).

    Fields: minute  hour  day  month  weekday (0=Sun 1=Mon … 6=Sat)
    Iterates minute-by-minute from (after + 1 min); caps at 366 days.
    Returns None on invalid expression.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        logger.warning("cron_next: invalid expression %r", expr)
        return None
    f_min, f_hour, f_day, f_month, f_wday = parts

    base = (after or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    current = base + timedelta(minutes=1)

    for _ in range(366 * 24 * 60):
        # Python weekday(): 0=Mon … 6=Sun → cron: 0=Sun 1=Mon … 6=Sat
        cron_wd = (current.weekday() + 1) % 7
        if (
            _field_matches(current.minute,  f_min)
            and _field_matches(current.hour,  f_hour)
            and _field_matches(current.day,   f_day)
            and _field_matches(current.month, f_month)
            and _field_matches(cron_wd,       f_wday)
        ):
            return current
        current += timedelta(minutes=1)
    return None


def _parse_interval_delta(schedule: dict) -> timedelta | None:
    """Convert an interval schedule dict to a timedelta.

    Accepts:
      {"type": "interval", "value": 30, "unit": "minutes"}
      {"type": "interval", "value": 1,  "unit": "days"}
    Units: minutes, hours, days, weeks
    """
    value = int(schedule.get("value", 0))
    unit = schedule.get("unit", "minutes").lower().rstrip("s")  # normalise plural
    if not value:
        return None
    mapping = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1),
               "day": timedelta(days=1), "week": timedelta(weeks=1)}
    unit_td = mapping.get(unit)
    if unit_td is None:
        return None
    return unit_td * value


def compute_next_due(schedule: dict, after: datetime | None = None) -> str | None:
    """Return ISO 8601 UTC string for the next scheduled run.

    schedule types:
      cron      — {"type":"cron", "cron":"30 7 * * 1-5"}
      interval  — {"type":"interval", "value":30, "unit":"minutes"}
      one_time  — {"type":"one_time", "at":"2026-03-15T08:00:00Z"}
    Returns None if unable to compute (expression error, one_time already past).
    """
    stype = schedule.get("type", "")
    now = after or datetime.now(timezone.utc)

    if stype == "cron":
        dt = cron_next(schedule.get("cron", ""), after=now)
        return dt.isoformat() if dt else None

    if stype == "interval":
        delta = _parse_interval_delta(schedule)
        if delta is None:
            return None
        return (now + delta).isoformat()

    if stype == "one_time":
        at_str = schedule.get("at", "")
        try:
            at_dt = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
            if at_dt <= now:
                return None  # already past
            return at_dt.isoformat()
        except (ValueError, TypeError):
            return None

    return None


# ── Telegram notification ─────────────────────────────────────────────────────

async def _notify_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("TaskScheduler: Telegram notification failed: %s", e)


# ── Task Scheduler ────────────────────────────────────────────────────────────

class TaskScheduler:
    """Data-driven task scheduler backed by Qdrant.

    Injected dependencies:
      qdrant       — QdrantAdapter (already initialised)
      cog          — CognitionEngine (for parse_task_intent)
      _dispatch_fn — ExecutionEngine._dispatch (injected post-init via set_dispatch_fn)
    """

    SCHEDULER_INTERVAL = 60  # seconds between due-task checks

    def __init__(self, qdrant, cog):
        self.qdrant = qdrant
        self.cog = cog
        self._dispatch_fn = None   # injected from main.py after exec_engine is ready
        self._running: set = set() # task_ids currently executing (prevent double-run)

    def set_dispatch_fn(self, fn) -> None:
        self._dispatch_fn = fn

    # ── NL intent parsing ─────────────────────────────────────────────────

    async def parse_task_nl(self, user_input: str) -> dict:
        """Call the LLM to extract a structured TaskDefinition from NL.

        Returns a dict with keys:
          needs_clarification (bool)
          clarification_question (str, if needs_clarification)
          title, schedule, steps, notify_when, stop_condition
        On failure returns {"error": ...}.
        """
        try:
            return await self.cog.parse_task_intent(user_input)
        except Exception as e:
            return {"error": f"Task parsing failed: {e}"}

    # ── Capability checking ───────────────────────────────────────────────

    def check_capabilities(self, steps: list) -> dict:
        """Return {"capable": True} or {"capable": False, "missing": [...], "blockers": [...]}.

        Checks env-var availability of each adapter required by the step intents.
        Does not make live connections.
        """
        missing = []
        for step in steps:
            intent = step.get("intent", "")
            check = _CAPABILITY.get(intent)
            if check is not None and not check():
                name = _ADAPTER_NAMES.get(intent, intent)
                if name not in missing:
                    missing.append(name)
        if missing:
            blockers = [f"{m} adapter not configured (missing env vars)" for m in missing]
            return {"capable": False, "missing": missing, "blockers": blockers}
        return {"capable": True}

    # ── Task storage ──────────────────────────────────────────────────────

    async def store_task(self, task_def: dict, human_confirmed: bool = False) -> dict:
        """Write a validated task to Qdrant.

        PROSPECTIVE — scheduling metadata (when / status / next_due)
        PROCEDURAL  — execution steps (requires human_confirmed=True)

        Both entries share the same task_id.
        Returns {status, task_id, next_due, title}.
        """
        task_id    = str(uuid.uuid4())
        title      = task_def.get("title", "Unnamed task")
        schedule   = task_def.get("schedule", {})
        steps      = task_def.get("steps", [])
        notify_when = task_def.get("notify_when", "always")
        stop_condition = task_def.get("stop_condition")

        next_due = compute_next_due(schedule)
        if not next_due and schedule.get("type") != "one_time":
            return {"status": "error",
                    "error": "Cannot compute next_due from schedule — check cron/interval format"}

        now_iso = datetime.now(timezone.utc).isoformat()
        capabilities_needed = list({
            _ADAPTER_NAMES.get(s.get("intent", ""), s.get("intent", ""))
            for s in steps
        })

        # ── PROSPECTIVE: scheduling metadata ─────────────────────────────
        prospective_content = (
            f"Scheduled task: {title}. "
            f"Schedule: {json.dumps(schedule)}. "
            f"Notify: {notify_when}."
        )
        try:
            await self.qdrant.store(
                content=prospective_content,
                metadata={
                    "type": "scheduled_task",
                    "task_id": task_id,
                    "title": title,
                    "schedule": schedule,
                    "next_due": next_due,
                    "status": "active",
                    "notify_when": notify_when,
                    "stop_condition": stop_condition,
                    "capabilities": capabilities_needed,
                    "created_at": now_iso,
                    "last_run": None,
                    "run_count": 0,
                },
                collection=PROSPECTIVE,
                writer="sovereign-core",
            )
        except Exception as e:
            return {"status": "error", "error": f"Failed to write PROSPECTIVE: {e}"}

        # ── PROCEDURAL: execution steps ───────────────────────────────────
        procedural_content = (
            f"Task procedure for '{title}': "
            + "; ".join(s.get("description", s.get("intent", "")) for s in steps)
        )
        try:
            await self.qdrant.store(
                content=procedural_content,
                metadata={
                    "type": "task_procedure",
                    "task_id": task_id,
                    "title": title,
                    "steps": steps,
                    "created_at": now_iso,
                },
                collection=PROCEDURAL,
                writer="sovereign-core",
                human_confirmed=human_confirmed,
            )
        except Exception as e:
            # Prospective already written — task is partially stored; warn but continue
            logger.warning("TaskScheduler: PROCEDURAL write failed for %s: %s", task_id, e)
            return {
                "status": "partial",
                "task_id": task_id,
                "next_due": next_due,
                "title": title,
                "warning": f"Scheduling metadata saved but procedure not persisted: {e}",
            }

        logger.info("TaskScheduler: stored task %s '%s' next_due=%s", task_id, title, next_due)
        return {
            "status": "ok",
            "task_id": task_id,
            "title": title,
            "next_due": next_due,
            "schedule": schedule,
            "steps_count": len(steps),
            "notify_when": notify_when,
            "capabilities": capabilities_needed,
        }

    # ── Task listing ──────────────────────────────────────────────────────

    async def list_tasks(self, status_filter: str = "active") -> dict:
        """Return all scheduled tasks from PROSPECTIVE, optionally filtered by status.

        status_filter: "active" | "paused" | "cancelled" | "all"
        """
        try:
            all_items, _ = await self.qdrant.client.scroll(
                collection_name=PROSPECTIVE,
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            return {"status": "error", "error": f"Qdrant scroll failed: {e}"}

        tasks = []
        for item in all_items:
            payload = dict(item.payload or {})
            if payload.get("type") != "scheduled_task":
                continue
            if status_filter != "all" and payload.get("status") != status_filter:
                continue
            tasks.append({
                "task_id":    payload.get("task_id"),
                "title":      payload.get("title"),
                "status":     payload.get("status"),
                "schedule":   payload.get("schedule"),
                "next_due":   payload.get("next_due"),
                "last_run":   payload.get("last_run"),
                "run_count":  payload.get("run_count", 0),
                "notify_when": payload.get("notify_when"),
            })
        tasks.sort(key=lambda t: t.get("next_due") or "")
        return {"status": "ok", "tasks": tasks, "count": len(tasks), "filter": status_filter}

    # ── Task status update ────────────────────────────────────────────────

    async def update_task_status(self, task_id: str, new_status: str) -> dict:
        """Set status on a scheduled_task in PROSPECTIVE.

        new_status: "active" | "paused" | "cancelled"
        Returns {status, task_id, new_status} or {status: "error"}.
        """
        point_id = await self._find_point_id(PROSPECTIVE, task_id)
        if not point_id:
            return {"status": "error", "error": f"Task {task_id} not found in PROSPECTIVE"}
        try:
            await self.qdrant.client.set_payload(
                collection_name=PROSPECTIVE,
                payload={"status": new_status},
                points=[point_id],
            )
            logger.info("TaskScheduler: task %s set to %s", task_id, new_status)
            return {"status": "ok", "task_id": task_id, "new_status": new_status}
        except Exception as e:
            return {"status": "error", "error": f"Failed to update status: {e}"}

    # ── Scheduler execution loop ──────────────────────────────────────────

    async def run_due_tasks(self) -> list:
        """Execute all tasks whose next_due <= now(UTC) and status=="active".

        Returns list of {task_id, title, outcome} for tasks that ran.
        """
        if not self._dispatch_fn:
            logger.warning("TaskScheduler: no dispatch_fn — skipping run")
            return []

        due = await self._get_due_tasks()
        ran = []
        for prospective in due:
            task_id = prospective.get("task_id")
            if not task_id or task_id in self._running:
                continue
            procedure = await self._get_procedure(task_id)
            if not procedure:
                logger.warning("TaskScheduler: no procedure found for task %s — skipping", task_id)
                continue
            self._running.add(task_id)
            try:
                outcome = await self._execute_task(prospective, procedure)
                ran.append({"task_id": task_id, "title": prospective.get("title"), **outcome})
            except Exception as e:
                logger.error("TaskScheduler: task %s failed: %s", task_id, e)
                ran.append({"task_id": task_id, "title": prospective.get("title"),
                            "outcome": "error", "error": str(e)})
            finally:
                self._running.discard(task_id)
        return ran

    async def _execute_task(self, prospective: dict, procedure: dict) -> dict:
        """Run one task: execute steps, evaluate conditions, notify, persist.

        Returns {"outcome": "positive"|"negative"|"no_findings", "steps_run": int}.
        """
        task_id    = prospective["task_id"]
        title      = prospective.get("title", task_id)
        steps      = procedure.get("steps", [])
        notify_when = prospective.get("notify_when", "always")
        stop_cond  = prospective.get("stop_condition")
        schedule   = prospective.get("schedule", {})

        logger.info("TaskScheduler: running task %s '%s' (%d steps)", task_id, title, len(steps))

        step_results = []
        stop_triggered = False

        for step in steps:
            intent = step.get("intent", "")
            params = step.get("params", {})
            description = step.get("description", intent)

            # Build action dict from INTENT_ACTION_MAP (imported locally to avoid circular)
            from execution.engine import INTENT_ACTION_MAP
            base_action = INTENT_ACTION_MAP.get(intent, {"domain": "ollama", "operation": "query"})
            action = dict(base_action)
            action.update(params)   # merge step params (account, query, path, etc.)

            # Prompt is used for ollama/browser queries
            prompt = params.get("query") or params.get("prompt") or ""

            try:
                result = await self._dispatch_fn(
                    action, prompt,
                    security_confirmed=True,  # task was vetted at schedule-time
                )
                step_results.append({
                    "intent": intent,
                    "description": description,
                    "status": result.get("status", "unknown"),
                    "result": result,
                })
            except Exception as e:
                step_results.append({
                    "intent": intent,
                    "description": description,
                    "status": "error",
                    "error": str(e),
                })

            # Check stop condition after each step
            if stop_cond and self._evaluate_stop_condition(stop_cond, step_results):
                logger.info("TaskScheduler: stop condition met for task %s", task_id)
                stop_triggered = True
                break

        # Determine outcome and whether to notify
        has_content = any(
            r.get("status") == "ok"
            and (r.get("result", {}).get("count", 0) > 0
                 or r.get("result", {}).get("messages")
                 or r.get("result", {}).get("response")
                 or r.get("result", {}).get("results"))
            for r in step_results
        )
        errors = [r for r in step_results if r.get("status") == "error"]
        outcome = "negative" if errors and not has_content else ("positive" if has_content else "no_findings")

        should_notify = (
            notify_when == "always"
            or (notify_when == "on_findings" and has_content)
            or (notify_when == "on_error" and errors)
        )

        # Build summary for notification + episodic
        summary_parts = [f"*Scheduled task: {title}*", ""]
        for r in step_results:
            status_icon = "✅" if r.get("status") == "ok" else "❌"
            res = r.get("result", {})
            detail = (
                res.get("response", "")[:300]
                or (f"{res.get('count', 0)} results" if "count" in res else "")
                or (f"{len(res.get('messages', []))} messages" if "messages" in res else "")
                or r.get("error", "no detail")
            )
            summary_parts.append(f"{status_icon} {r['description']}: {detail}")

        if stop_triggered:
            summary_parts.append("\n_Stop condition met — task halted early._")

        summary = "\n".join(summary_parts)

        if should_notify:
            await _notify_telegram(summary[:4000])  # Telegram limit

        # Write episodic memory entry
        await self._write_episodic(task_id, title, outcome, summary, len(step_results))

        # Advance next_due (recurring tasks only — not one_time)
        now = datetime.now(timezone.utc)
        if schedule.get("type") != "one_time" and not stop_triggered:
            next_due = compute_next_due(schedule, after=now)
            await self._update_prospective_after_run(task_id, next_due, now.isoformat())
        elif schedule.get("type") == "one_time" or stop_triggered:
            # Mark completed
            point_id = await self._find_point_id(PROSPECTIVE, task_id)
            if point_id:
                new_status = "completed" if not stop_triggered else "completed"
                await self.qdrant.client.set_payload(
                    collection_name=PROSPECTIVE,
                    payload={"status": new_status, "last_run": now.isoformat()},
                    points=[point_id],
                )

        return {"outcome": outcome, "steps_run": len(step_results),
                "notified": should_notify, "stop_triggered": stop_triggered}

    def _evaluate_stop_condition(self, condition: str, step_results: list) -> bool:
        """Evaluate a natural-language stop condition against step results.

        Supported patterns:
          "on_first_result"   — stop as soon as any step returned content
          "on_error"          — stop as soon as any step errored
          "no_results"        — stop if the last step returned no content
        Returns True to stop, False to continue.
        """
        c = condition.lower().strip()
        if c == "on_first_result":
            return any(r.get("status") == "ok" and r.get("result", {}).get("count", 0) > 0
                       for r in step_results)
        if c == "on_error":
            return any(r.get("status") == "error" for r in step_results)
        # Default: do not stop
        return False

    # ── Qdrant helpers ────────────────────────────────────────────────────

    async def _get_due_tasks(self) -> list[dict]:
        """Scroll PROSPECTIVE for active scheduled tasks with next_due <= now(UTC)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            items, _ = await self.qdrant.client.scroll(
                collection_name=PROSPECTIVE,
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.error("TaskScheduler: PROSPECTIVE scroll failed: %s", e)
            return []

        due = []
        for item in items:
            payload = dict(item.payload or {})
            if payload.get("type") != "scheduled_task":
                continue
            if payload.get("status") != "active":
                continue
            next_due = payload.get("next_due", "")
            if next_due and next_due <= now_iso:
                due.append(payload)
        return due

    async def _get_procedure(self, task_id: str) -> dict | None:
        """Find the PROCEDURAL entry for a given task_id by payload scroll."""
        try:
            items, _ = await self.qdrant.client.scroll(
                collection_name=PROCEDURAL,
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.error("TaskScheduler: PROCEDURAL scroll failed: %s", e)
            return None

        for item in items:
            payload = dict(item.payload or {})
            if payload.get("task_id") == task_id and payload.get("type") == "task_procedure":
                return payload
        return None

    async def _find_point_id(self, collection: str, task_id: str) -> str | None:
        """Scroll collection to find the Qdrant point ID for a given task_id."""
        try:
            items, _ = await self.qdrant.client.scroll(
                collection_name=collection,
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return None
        for item in items:
            payload = dict(item.payload or {})
            if payload.get("task_id") == task_id:
                return str(item.id)
        return None

    async def _update_prospective_after_run(self, task_id: str,
                                             next_due: str | None,
                                             last_run: str) -> None:
        """Update next_due, last_run, run_count in the PROSPECTIVE entry."""
        point_id = await self._find_point_id(PROSPECTIVE, task_id)
        if not point_id:
            return
        # Fetch current run_count
        try:
            items, _ = await self.qdrant.client.scroll(
                collection_name=PROSPECTIVE,
                limit=200,
                with_payload=True,
                with_vectors=False,
            )
            run_count = 0
            for item in items:
                if str(item.id) == point_id:
                    run_count = (item.payload or {}).get("run_count", 0)
                    break
        except Exception:
            run_count = 0

        updates = {"last_run": last_run, "run_count": run_count + 1}
        if next_due:
            updates["next_due"] = next_due
        try:
            await self.qdrant.client.set_payload(
                collection_name=PROSPECTIVE,
                payload=updates,
                points=[point_id],
            )
        except Exception as e:
            logger.warning("TaskScheduler: failed to update next_due for %s: %s", task_id, e)

    async def _write_episodic(self, task_id: str, title: str,
                               outcome: str, summary: str, steps_run: int) -> None:
        """Write a run-history entry to EPISODIC."""
        try:
            await self.qdrant.store(
                content=f"Scheduled task run: {title}. Outcome: {outcome}. Steps: {steps_run}.",
                metadata={
                    "type": "task_run",
                    "task_id": task_id,
                    "title": title,
                    "outcome": outcome,
                    "steps_run": steps_run,
                    "summary_preview": summary[:300],
                    "run_at": datetime.now(timezone.utc).isoformat(),
                },
                collection=EPISODIC,
                writer="sovereign-core",
            )
        except Exception as e:
            logger.warning("TaskScheduler: episodic write failed for %s: %s", task_id, e)

    # ── Background loop ───────────────────────────────────────────────────

    async def _loop(self) -> None:
        await asyncio.sleep(30)  # initial delay — let services settle
        while True:
            try:
                ran = await self.run_due_tasks()
                if ran:
                    logger.info("TaskScheduler: ran %d task(s) this cycle: %s",
                                len(ran), [r.get("task_id") for r in ran])
            except Exception as e:
                logger.error("TaskScheduler: loop error: %s", e)
            await asyncio.sleep(self.SCHEDULER_INTERVAL)

    def start(self) -> asyncio.Task:
        task = asyncio.create_task(self._loop())
        logger.info("TaskScheduler: background loop started (interval: %ds)",
                    self.SCHEDULER_INTERVAL)
        return task
