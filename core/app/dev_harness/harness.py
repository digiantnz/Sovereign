"""
Dev-Harness Phase orchestrator.

Phase 1 (Analyse): deterministic — broker dispatches pylint + semgrep +
  boundary_scanner + GitHub Actions annotations. Gate is final.
  Run via: dev_analyse broker command (wrapper script dev_analyse.sh).

Phase 2 (Classify): LLM advisory — Ollama (llama3.1:8b) classifies findings.
  If gate==ESCALATE or boundary_violation found: escalate to Claude with diff.
  Implemented in classifier.py (step 7). Fallback stub used until then.

Phase 3 (Plan): surfaces to Director via prospective memory +
  Telegram in plain English. Writes status="pending_director_approval".

Phase 4 (Execute): Director has approved. Generates CC runsheet. Writes
  runsheet to prospective memory with _dev_runsheet=True. Director pastes to CC.

WM session key:  dev_harness:session
WM flag:         _developer_harness_checkpoint
PROSPECTIVE key: _dev_plan (Phase 3), _dev_runsheet (Phase 4)

LLM/deterministic boundary invariant:
  Phase 1 contains NO LLM calls. Gate decision is final and is written to
  the WM checkpoint before Phase 2 is invoked. Phase 2 advisory output
  never alters the gate_decision field in the checkpoint.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Working memory checkpoint flag — identifies this harness's session record.
_WM_FLAG        = "_developer_harness_checkpoint"
_WM_CONTENT_KEY = "dev_harness:session"

# Broker timeout for the full analysis run (pylint + semgrep + boundary can be slow).
_ANALYSE_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
# DevHarness
# ---------------------------------------------------------------------------

class DevHarness:
    """
    Orchestrates the four Dev-Harness phases.

    Dependencies injected by engine.py:
      broker  — BrokerAdapter (dispatches dev_analyse and friends)
      qdrant  — QdrantAdapter (working memory + prospective collection)
      github_token — optional GitHub PAT for Phase 1 Actions annotations
    """

    def __init__(self, broker, qdrant, github_token: str = "", cog=None):
        self.broker        = broker
        self.qdrant        = qdrant
        self._github_token = github_token
        self.cog           = cog  # CognitionEngine — required for DCL-gated Claude escalation in Phase 2

    # ──────────────────────────────────────────────────────────────────────
    # WM checkpoint helpers
    # Uses qdrant.client directly (the working-memory QdrantContainer).
    # Do NOT use qdrant.wm_client — that attribute does not exist on
    # QdrantAdapter and accessing it silently fails (caught by except).
    # ──────────────────────────────────────────────────────────────────────

    async def _load_checkpoint(self) -> dict | None:
        """Scroll working_memory for the dev harness checkpoint. Returns payload or None."""
        if not self.qdrant:
            return None
        try:
            from execution.adapters.qdrant import WORKING
            offset = None
            while True:
                result, next_offset = await self.qdrant.client.scroll(
                    collection_name=WORKING,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for r in result:
                    p = dict(r.payload or {})
                    if p.get(_WM_FLAG):
                        return p
                if next_offset is None:
                    return None
                offset = next_offset
        except Exception as e:
            logger.warning("DevHarness: _load_checkpoint failed: %s", e)
            return None

    async def _load_checkpoint_by_session(self, session_id_short: str) -> dict | None:
        """Return checkpoint whose session_id starts with session_id_short, or None."""
        cp = await self._load_checkpoint()
        if cp and cp.get("session_id", "").startswith(session_id_short):
            return cp
        return None

    async def _save_checkpoint(self, checkpoint: dict) -> None:
        """Delete any existing dev harness checkpoint(s) and write a fresh one."""
        if not self.qdrant:
            return
        try:
            from execution.adapters.qdrant import WORKING
            # Collect IDs of existing checkpoints
            offset = None
            to_delete: list = []
            while True:
                result, next_offset = await self.qdrant.client.scroll(
                    collection_name=WORKING,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for r in result:
                    if (r.payload or {}).get(_WM_FLAG):
                        to_delete.append(r.id)
                if next_offset is None:
                    break
                offset = next_offset
            if to_delete:
                await self.qdrant.client.delete(
                    collection_name=WORKING,
                    points_selector=to_delete,
                )
            # Embed and write fresh checkpoint
            await self.qdrant.store(
                content=_WM_CONTENT_KEY,
                metadata={**checkpoint, _WM_FLAG: True, "type": "developer_harness_checkpoint"},
                collection=WORKING,
            )
        except Exception as e:
            logger.warning("DevHarness: _save_checkpoint failed: %s", e)

    async def clear_checkpoint(self) -> int:
        """
        Delete all dev harness checkpoint points from working_memory.
        Returns count of deleted points.
        Called by dev_clear intent and run_reject.
        """
        if not self.qdrant:
            return 0
        try:
            from execution.adapters.qdrant import WORKING
            offset = None
            to_delete: list = []
            while True:
                result, next_offset = await self.qdrant.client.scroll(
                    collection_name=WORKING,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for r in result:
                    if (r.payload or {}).get(_WM_FLAG):
                        to_delete.append(r.id)
                if next_offset is None:
                    break
                offset = next_offset
            if to_delete:
                await self.qdrant.client.delete(
                    collection_name=WORKING,
                    points_selector=to_delete,
                )
            return len(to_delete)
        except Exception as e:
            logger.warning("DevHarness: clear_checkpoint failed: %s", e)
            return 0

    # ──────────────────────────────────────────────────────────────────────
    # Phase 1 — Analyse
    # ──────────────────────────────────────────────────────────────────────

    async def run_phase1(self, trigger: str = "explicit", skill_snapshot: dict | None = None, harness_snapshot: dict | None = None) -> dict:
        """
        Phase 1: Analyse. Deterministic — no LLM calls.

        Dispatches broker dev_analyse (wraps pylint + semgrep + boundary_scanner)
        then fetches optional GitHub Actions annotations. Finalises AnalysisResult,
        saves WM checkpoint, auto-triggers Phase 2 if gate != APPROVE.

        trigger: "explicit" (Director request) | "nightly" (scheduler) | "verify" (re-check).
        Returns result dict for engine.py to use as result_for_translator.

        Graceful error handling:
          - Broker 503: toolchain not enabled → synthesises a high-severity Finding.
          - Broker error: logs tool_error, analysis continues with partial results.
          - GitHub failure: logged, Phase 1 continues with local results only.
        """
        from dev_harness.analyser import (
            AnalysisResult, Finding, GateDecision,
            parse_pylint_output, parse_semgrep_output, parse_boundary_output,
        )
        from dev_harness.github_client import get_latest_run_annotations

        session_id       = str(uuid.uuid4())
        session_id_short = session_id[:8]
        now              = datetime.now(timezone.utc).isoformat()
        scan_root        = "/hostfs/home/sovereign/sovereign/core/app"

        result = AnalysisResult(
            session_id = session_id,
            trigger    = trigger,
            scan_root  = scan_root,
        )

        # ── Broker dispatch ────────────────────────────────────────────────
        broker_resp = await self.broker.exec_command(
            "dev_analyse",
            timeout=_ANALYSE_TIMEOUT_S,
        )

        http_status = broker_resp.get("http_status", 200)

        if http_status == 503:
            # Toolchain not yet enabled — surface actionable Finding
            _disabled = Finding(
                source   = "broker",
                type     = "boundary",
                file     = "",
                line     = 0,
                message  = (
                    "Broker dev toolchain not enabled — rebuild required. "
                    "Run: docker compose build docker-broker "
                    "&& docker compose up -d docker-broker"
                ),
                severity = "high",
                rule_id  = "TOOLCHAIN_DISABLED",
            )
            result.findings.append(_disabled)
            result.tool_errors.append("dev toolchain disabled (HTTP 503)")
            logger.warning("DevHarness Phase1: broker returned 503 — toolchain not enabled")

        elif broker_resp.get("status") == "error" or (http_status and http_status not in (200,)):
            err_msg = broker_resp.get("error") or f"broker HTTP {http_status}"
            result.tool_errors.append(f"broker dispatch failed: {err_msg}")
            logger.warning("DevHarness Phase1: broker dev_analyse error: %s", err_msg)

        else:
            # Parse NDJSON tool-envelope lines from dev_analyse.sh
            stdout = broker_resp.get("stdout", "") or ""
            for raw_line in stdout.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    env = json.loads(raw_line)
                except json.JSONDecodeError:
                    result.tool_errors.append(f"envelope parse error: {raw_line[:80]}")
                    continue

                tool       = env.get("tool", "")
                tool_out   = env.get("stdout", "") or ""
                tool_err   = env.get("stderr", "") or ""
                tool_exit  = env.get("exit_code", 0)

                # Non-zero exit with empty stdout = tool invocation failed
                if tool_exit != 0 and not tool_out.strip():
                    result.tool_errors.append(
                        f"{tool}: {tool_err[:200] if tool_err else 'exited non-zero'}"
                    )
                    continue

                if tool == "pylint":
                    _findings = parse_pylint_output(tool_out, scan_root)
                    result.findings.extend(_findings)
                    result.local_count += len(_findings)
                    if tool_err:
                        logger.debug("DevHarness Phase1: pylint stderr: %s", tool_err[:200])

                elif tool == "semgrep":
                    _findings = parse_semgrep_output(tool_out, scan_root)
                    result.findings.extend(_findings)
                    result.local_count += len(_findings)
                    if tool_err:
                        logger.debug("DevHarness Phase1: semgrep stderr: %s", tool_err[:200])

                elif tool == "boundary":
                    _findings = parse_boundary_output(tool_out)
                    result.findings.extend(_findings)
                    result.local_count += len(_findings)
                    if tool_err:
                        logger.warning("DevHarness Phase1: boundary_scanner stderr: %s", tool_err[:200])

                else:
                    logger.warning("DevHarness Phase1: unknown tool envelope: %s", tool)

        # ── GitHub Actions annotations ─────────────────────────────────────
        github_token = self._github_token or os.environ.get("GITHUB_TOKEN", "")
        if github_token:
            try:
                gh_findings = await get_latest_run_annotations(github_token)
                result.findings.extend(gh_findings)
                result.github_count = len(gh_findings)
            except Exception as e:
                result.tool_errors.append(f"github_client: {e}")
                logger.warning("DevHarness Phase1: github_client error: %s", e)

        # ── Finalise (scoring + gate — deterministic) ──────────────────────
        result.finalise()

        # ── Build WM checkpoint ────────────────────────────────────────────
        n_by_sev  = _count_by_severity(result.findings)
        checkpoint = {
            "session_id":       session_id,
            "session_id_short": session_id_short,
            "trigger":          trigger,
            "current_step":     "analyse",
            "phase_index":      0,  # 0-based index into phases array; DAG renderer uses this directly
            "step_results": {
                "analyse": {
                    "total_score":        result.total_score,
                    "gate_decision":      result.gate_decision.value,
                    "finding_count":      len(result.findings),
                    "local_count":        result.local_count,
                    "github_count":       result.github_count,
                    "tool_errors":        result.tool_errors,
                    "severity_breakdown": n_by_sev,
                    # Full findings stored in checkpoint for Phase 2/4 retrieval
                    "findings_json":      [f.to_dict() for f in result.findings],
                    "ts":                 now,
                },
            },
            "last_checkpoint_ts": now,
            # Self-awareness snapshots — fetched before analysis, passed by engine.py
            "developer_harness:skill_snapshot":   skill_snapshot or {},
            "developer_harness:harness_snapshot": harness_snapshot or {},
        }
        await self._save_checkpoint(checkpoint)

        # Step 9 — episodic write (fire-and-forget; never blocks Phase 1 return)
        try:
            import asyncio as _aio_ep
            from dev_harness.memory import write_episodic_analysis as _write_ep
            _aio_ep.create_task(_write_ep(
                self.qdrant, session_id_short, trigger,
                result.gate_decision.value, result.total_score,
                len(result.findings), n_by_sev, result.tool_errors,
            ))
        except Exception as _ep_err:
            logger.warning("DevHarness Phase1: episodic task creation failed: %s", _ep_err)

        logger.info(
            "DevHarness Phase1 done — session=%s trigger=%s gate=%s score=%d findings=%d",
            session_id_short, trigger, result.gate_decision.value,
            result.total_score, len(result.findings),
        )

        # ── Auto-chain: Phase 2 → 3 if gate != APPROVE ────────────────────
        # trigger is passed explicitly so run_phase2 can gate Phase 3.
        # Nightly runs suppress Phase 3 on REVISE and never auto-chain to
        # prospective memory — Director is notified only on BLOCK/ESCALATE.
        phase2_result: dict = {}
        if result.gate_decision != GateDecision.APPROVE:
            phase2_result = await self.run_phase2(result, checkpoint, trigger=trigger)

        return {
            "success":           True,
            "phase":             "analyse",
            "session_id":        session_id,
            "session_id_short":  session_id_short,
            "trigger":           trigger,
            "gate_decision":     result.gate_decision.value,
            "total_score":       result.total_score,
            "finding_count":     len(result.findings),
            "severity_breakdown": n_by_sev,
            "tool_errors":       result.tool_errors,
            "top_findings":      _top_findings(result.findings, n=5),
            **({"phase2": phase2_result} if phase2_result else {}),
        }

    # ──────────────────────────────────────────────────────────────────────
    # Phase 2 — Classify
    # ──────────────────────────────────────────────────────────────────────

    async def run_phase2(self, result, checkpoint: dict, trigger: str = "explicit") -> dict:
        """
        Phase 2: Classify. LLM advisory — does NOT change gate_decision.

        Imports from dev_harness.classifier (step 7).
        Falls back to _stub_classification() when classifier.py is not yet available.

        LLM boundary: gate_decision in the checkpoint is read-only here.
        Phase 2 output is advisory only — it updates the 'classify' step_results
        entry but never overwrites 'gate_decision'.

        trigger controls Phase 3 chaining:
          "explicit" — Phase 3 always runs (prospective write + Telegram).
          "nightly"  — Phase 3 runs ONLY on BLOCK or ESCALATE.
                       REVISE is silent on nightly (no prospective write, no Telegram).
                       This is an explicit flag, not inferred from context — a future
                       code change MUST pass trigger="nightly" to suppress Phase 3.
        """
        _skill_snap   = checkpoint.get("developer_harness:skill_snapshot", {})
        _harness_snap = checkpoint.get("developer_harness:harness_snapshot", {})
        try:
            from dev_harness.classifier import classify as _classify
            classification = await _classify(result, self.qdrant, cog=self.cog,
                                             skill_snapshot=_skill_snap, harness_snapshot=_harness_snap)
        except ImportError:
            logger.info(
                "DevHarness Phase2: classifier.py not yet available (step 7) — using stub"
            )
            classification = _stub_classification(result)
        except Exception as e:
            logger.warning("DevHarness Phase2: classifier error: %s", e)
            classification = _stub_classification(result)

        now = datetime.now(timezone.utc).isoformat()

        # Update checkpoint — gate_decision is immutable, only advisory is added
        checkpoint.setdefault("step_results", {})
        checkpoint["step_results"]["classify"] = {
            "advisory":             classification.get("advisory", ""),
            "escalated_to_claude":  classification.get("escalated_to_claude", False),
            "suggested_fixes":      classification.get("suggested_fixes", []),
            "ts":                   now,
        }
        checkpoint["current_step"]       = "classify"
        checkpoint["phase_index"]        = 1  # classify is phase index 1
        checkpoint["last_checkpoint_ts"] = now
        await self._save_checkpoint(checkpoint)

        # Auto-chain Phase 3 — gated on trigger
        # "explicit": always chain Phase 3 (prospective write + Telegram to Director)
        # "nightly":  chain Phase 3 ONLY on BLOCK or ESCALATE; REVISE is silent.
        #             This gate is checked against the EXPLICIT trigger parameter —
        #             never inferred from result content — so a future change that
        #             removes the trigger="nightly" param would re-enable Phase 3.
        from dev_harness.analyser import GateDecision as _GD
        if trigger == "nightly" and result.gate_decision not in (_GD.BLOCK, _GD.ESCALATE):
            # REVISE on nightly: log, update checkpoint, return without Phase 3
            logger.info(
                "DevHarness nightly Phase2: gate=%s — Phase 3 suppressed "
                "(nightly only notifies on block/escalate)",
                result.gate_decision.value,
            )
            phase3_result = {
                "status": "nightly_silent",
                "gate":   result.gate_decision.value,
                "reason": "nightly run — Phase 3 only fires on block/escalate",
            }
        else:
            phase3_result = await self.run_phase3(result, classification, checkpoint)

        return {
            "advisory":            classification.get("advisory", ""),
            "escalated_to_claude": classification.get("escalated_to_claude", False),
            "phase3":              phase3_result,
        }

    # ──────────────────────────────────────────────────────────────────────
    # Phase 3 — Plan
    # ──────────────────────────────────────────────────────────────────────

    async def run_phase3(self, result, classification: dict, checkpoint: dict) -> dict:
        """
        Phase 3: Plan. Surfaces findings to Director.

        Writes a 'pending_director_approval' entry to prospective memory
        and sends a Telegram notification with approve/reject instructions.
        """
        from dev_harness.analyser import AnalysisResult
        session_id       = checkpoint["session_id"]
        session_id_short = checkpoint["session_id_short"]
        gate             = result.gate_decision.value
        now              = datetime.now(timezone.utc).isoformat()

        n_by_sev    = _count_by_severity(result.findings)
        top5        = _top_findings(result.findings, n=5)
        advisory    = classification.get("advisory", "")
        plan_summary = _build_plan_summary(result, n_by_sev, top5, advisory)

        # Write pending_director_approval entry to prospective memory
        prospective_point_id = ""
        try:
            from execution.adapters.qdrant import PROSPECTIVE
            prospective_point_id = await self.qdrant.store(
                content=plan_summary,
                metadata={
                    "type":              "dev_plan",
                    "_dev_plan":         True,
                    "session_id":        session_id,
                    "session_id_short":  session_id_short,
                    "gate_decision":     gate,
                    "total_score":       result.total_score,
                    "finding_count":     len(result.findings),
                    "severity_breakdown": n_by_sev,
                    "suggested_fixes":   classification.get("suggested_fixes", []),
                    "status":            "pending_director_approval",
                    "trigger":           result.trigger,
                    "timestamp":         now,
                },
                collection=PROSPECTIVE,
            )
        except Exception as e:
            logger.warning("DevHarness Phase3: prospective write failed: %s", e)

        # Telegram notification
        tg_msg = _build_phase3_telegram(gate, session_id_short, result, n_by_sev, top5)
        await self._telegram_notify(tg_msg)

        # Update checkpoint
        checkpoint.setdefault("step_results", {})
        checkpoint["step_results"]["plan"] = {
            "prospective_point_id": prospective_point_id,
            "plan_summary":         plan_summary[:500],
            "ts":                   now,
        }
        checkpoint["current_step"]       = "plan"
        checkpoint["phase_index"]        = 2  # plan is phase index 2
        checkpoint["last_checkpoint_ts"]  = now
        await self._save_checkpoint(checkpoint)

        # Step 9 — meta state update (fire-and-forget; gate is final, plan visible to Director)
        try:
            import asyncio as _aio_meta
            from dev_harness.memory import update_meta_state as _update_meta
            _aio_meta.create_task(_update_meta(
                self.qdrant, session_id_short, gate, result.total_score,
            ))
        except Exception as _meta_err:
            logger.warning("DevHarness Phase3: meta update task creation failed: %s", _meta_err)

        logger.info(
            "DevHarness Phase3 done — session=%s gate=%s prospective_id=%s",
            session_id_short, gate, prospective_point_id,
        )

        return {
            "status":               "pending_director_approval",
            "session_id_short":     session_id_short,
            "prospective_point_id": prospective_point_id,
            "plan_summary":         plan_summary[:500],
        }

    # ──────────────────────────────────────────────────────────────────────
    # Phase 4 — Execute (Director-approved)
    # ──────────────────────────────────────────────────────────────────────

    async def run_phase4(self, session_id_short: str) -> dict:
        """
        Phase 4: Execute. Director has approved via "approve dev fix {id}".

        Generates a structured CC runsheet from Phase 1 findings + Phase 2
        advisory, writes it to prospective memory (_dev_runsheet=True),
        and notifies Director via Telegram.

        The runsheet is a plain-text document the Director pastes into
        Claude Code to direct the actual fixes. Sovereign never self-modifies.
        """
        cp = await self._load_checkpoint_by_session(session_id_short)
        if cp is None:
            return {
                "success": False,
                "error": (
                    f"No dev harness session found matching '{session_id_short}'. "
                    "Run 'dev analyse' first."
                ),
            }

        current_step = cp.get("current_step", "")
        if current_step not in ("plan", "classify", "analyse"):
            return {
                "success": False,
                "error": (
                    f"Session {session_id_short} is at step '{current_step}', "
                    "not waiting for Director approval."
                ),
            }

        session_id = cp["session_id"]
        now = datetime.now(timezone.utc).isoformat()

        # Reconstruct Finding list from checkpoint
        from dev_harness.analyser import Finding
        analyse_result  = cp.get("step_results", {}).get("analyse", {})
        findings_raw    = analyse_result.get("findings_json", [])
        findings        = [Finding.from_dict(f) for f in findings_raw]
        classify_result = cp.get("step_results", {}).get("classify", {})
        suggested_fixes = classify_result.get("suggested_fixes", [])

        # Generate CC runsheet
        cc_runsheet = _generate_cc_runsheet(findings, suggested_fixes, session_id_short)

        # Write runsheet to prospective memory
        runsheet_point_id = ""
        try:
            from execution.adapters.qdrant import PROSPECTIVE
            runsheet_point_id = await self.qdrant.store(
                content=(
                    f"Dev-Harness CC runsheet for session {session_id_short}: "
                    f"{cc_runsheet['summary']}"
                ),
                metadata={
                    "type":             "dev_runsheet",
                    "_dev_runsheet":    True,
                    "session_id":       session_id,
                    "session_id_short": session_id_short,
                    "cc_runsheet":      cc_runsheet,
                    "status":           "pending_execution",
                    "timestamp":        now,
                },
                collection=PROSPECTIVE,
            )
        except Exception as e:
            logger.warning("DevHarness Phase4: runsheet write failed: %s", e)

        # Telegram
        tg_msg = (
            f"📋 *CC Runsheet Ready* — `{session_id_short}`\n\n"
            f"{cc_runsheet['summary']}\n\n"
            f"Retrieve with: `show dev runsheet {session_id_short}`\n"
            f"After fixes: `verify dev fix {session_id_short}`"
        )
        await self._telegram_notify(tg_msg)

        # Update checkpoint
        cp.setdefault("step_results", {})
        cp["step_results"]["execute"] = {
            "approved_at":      now,
            "runsheet_point_id": runsheet_point_id,
            "ts":               now,
        }
        cp["current_step"]       = "execute"
        cp["phase_index"]        = 4  # execute is phase index 4 (after HITL Approve at index 3)
        cp["last_checkpoint_ts"]  = now
        await self._save_checkpoint(cp)

        logger.info(
            "DevHarness Phase4 done — session=%s runsheet_id=%s",
            session_id_short, runsheet_point_id,
        )

        return {
            "success":           True,
            "phase":             "execute",
            "session_id_short":  session_id_short,
            "runsheet_point_id": runsheet_point_id,
            "cc_runsheet":       cc_runsheet,
        }

    # ──────────────────────────────────────────────────────────────────────
    # run_reject — Director rejected the plan
    # ──────────────────────────────────────────────────────────────────────

    async def run_reject(self, session_id_short: str) -> dict:
        """
        Director has rejected the plan via "reject dev fix {id}".
        Marks the prospective plan entry as cancelled and clears WM checkpoint.
        """
        cp = await self._load_checkpoint_by_session(session_id_short)
        if cp is None:
            return {"success": False, "error": f"No session matching '{session_id_short}'."}

        # Mark prospective plan entry as cancelled
        try:
            from execution.adapters.qdrant import PROSPECTIVE
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            result, _ = await self.qdrant.archive_client.scroll(
                collection_name=PROSPECTIVE,
                scroll_filter=Filter(must=[
                    FieldCondition(key="_dev_plan",         match=MatchValue(value=True)),
                    FieldCondition(key="session_id_short",  match=MatchValue(value=session_id_short)),
                ]),
                limit=10,
                with_payload=False,
                with_vectors=False,
            )
            for r in result:
                await self.qdrant.archive_client.set_payload(
                    collection_name=PROSPECTIVE,
                    payload={"status": "cancelled"},
                    points=[r.id],
                )
        except Exception as e:
            logger.warning("DevHarness run_reject: prospective update failed: %s", e)

        await self.clear_checkpoint()

        await self._telegram_notify(
            f"❌ *Dev fix rejected* — `{session_id_short}`\n"
            "Session cleared. Run 'dev analyse' when ready for a new cycle."
        )

        return {"success": True, "status": "rejected", "session_id_short": session_id_short}

    # ──────────────────────────────────────────────────────────────────────
    # run_verify — Phase 1 re-run after Director applies fixes
    # ──────────────────────────────────────────────────────────────────────

    async def run_verify(self, session_id_short: str) -> dict:
        """
        Verify dev fix: re-run Phase 1 after Director has applied the CC runsheet.

        Requires a checkpoint at step='execute' for the given session_id_short.
        Returns a fresh Phase 1 result with 'verifies_session' annotated.
        """
        cp = await self._load_checkpoint_by_session(session_id_short)
        if cp is None:
            return {
                "success": False,
                "error": (
                    f"No session matching '{session_id_short}'. "
                    "If the container restarted since Phase 4, run a fresh analysis."
                ),
            }
        if cp.get("current_step") != "execute":
            return {
                "success": False,
                "error": (
                    f"Session {session_id_short} is at step '{cp.get('current_step')}' "
                    "— verify is only valid after Phase 4 (execute)."
                ),
            }

        original_session_id = cp["session_id"]

        # Run a fresh Phase 1
        verify_result = await self.run_phase1(trigger="verify")
        verify_result["verifies_session"]    = session_id_short
        verify_result["original_session_id"] = original_session_id

        # Step 9 — semantic fix write.
        # Constraint 4: ONLY on verify_passed=True (gate==APPROVE on re-run).
        # Phase 3 Director approval alone does NOT qualify — the verify gate is
        # the confirmation that findings were actually resolved.
        if verify_result.get("gate_decision") == "approve":
            _analyse_step   = cp.get("step_results", {}).get("analyse", {})
            _finding_before = _analyse_step.get("finding_count", 0)
            _finding_after  = verify_result.get("finding_count", 0)
            _sev_after      = verify_result.get("severity_breakdown", {})
            _orig_gate      = _analyse_step.get("gate_decision", "unknown")
            try:
                import asyncio as _aio_sf
                from dev_harness.memory import write_semantic_fix as _write_sf
                _aio_sf.create_task(_write_sf(
                    self.qdrant, session_id_short,
                    _orig_gate, verify_result["gate_decision"],
                    _finding_before, _finding_after, _sev_after,
                ))
            except Exception as _sf_err:
                logger.warning(
                    "DevHarness run_verify: semantic fix task creation failed: %s", _sf_err
                )

        return verify_result

    # ──────────────────────────────────────────────────────────────────────
    # run_status — view current session
    # ──────────────────────────────────────────────────────────────────────

    async def run_status(self) -> dict:
        """Return the current harness session state from working memory."""
        cp = await self._load_checkpoint()
        if cp is None:
            return {
                "status":  "no_session",
                "message": "No active dev harness session. Run 'dev analyse' to start.",
            }
        analyse = cp.get("step_results", {}).get("analyse", {})
        return {
            "status":            "active",
            "session_id_short":  cp.get("session_id_short"),
            "current_step":      cp.get("current_step"),
            "trigger":           cp.get("trigger"),
            "last_checkpoint_ts": cp.get("last_checkpoint_ts"),
            "gate_decision":     analyse.get("gate_decision"),
            "total_score":       analyse.get("total_score"),
            "finding_count":     analyse.get("finding_count"),
            "severity_breakdown": analyse.get("severity_breakdown"),
            "tool_errors":       analyse.get("tool_errors", []),
        }

    # ──────────────────────────────────────────────────────────────────────
    # Telegram
    # ──────────────────────────────────────────────────────────────────────

    async def _telegram_notify(self, message: str) -> None:
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
        if not token or not chat_id:
            logger.warning("DevHarness: Telegram credentials missing — skipping notification")
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                )
        except Exception as e:
            logger.warning("DevHarness: Telegram notification failed: %s", e)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _count_by_severity(findings: list) -> dict:
    counts: dict = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _top_findings(findings: list, n: int = 5) -> list:
    """Return top N findings sorted descending by severity weight."""
    from dev_harness.analyser import SEVERITY_WEIGHTS
    sorted_f = sorted(
        findings,
        key=lambda f: SEVERITY_WEIGHTS.get(f.severity, 1),
        reverse=True,
    )
    return [f.to_dict() for f in sorted_f[:n]]


def _stub_classification(result) -> dict:
    """
    Fallback when classifier.py (step 7) is not yet implemented.
    Returns a safe minimal advisory derived purely from Phase 1 findings.
    LLM boundary: this function contains no LLM calls.
    """
    has_boundary = any(f.rule_id.startswith("B") for f in result.findings)
    advisory = (
        f"Gate: {result.gate_decision.value.upper()} (score {result.total_score}). "
        f"{len(result.findings)} findings. "
        + ("Boundary violations detected — B-rules require attention. " if has_boundary else "")
        + "LLM classification pending (classifier.py, step 7 not yet implemented)."
    )
    return {
        "advisory":            advisory,
        "escalated_to_claude": False,
        "suggested_fixes":     [],
    }


def _build_plan_summary(result, n_by_sev: dict, top5: list, advisory: str) -> str:
    lines = [
        f"Dev analysis {result.gate_decision.value.upper()}: score {result.total_score}",
        (
            f"Findings: {len(result.findings)} total "
            f"({n_by_sev['critical']} critical, {n_by_sev['high']} high, "
            f"{n_by_sev['medium']} medium, {n_by_sev['low']} low)"
        ),
    ]
    if advisory:
        lines.append(f"Advisory: {advisory}")
    if top5:
        lines.append("Top findings:")
        for f in top5[:3]:
            lines.append(
                f"  [{f['severity'].upper()}] {f['file']}:{f['line']} — {f['message'][:80]}"
            )
    return "\n".join(lines)


def _build_phase3_telegram(gate: str, session_id_short: str, result, n_by_sev: dict, top5: list) -> str:
    emoji = {"approve": "✅", "revise": "🔄", "block": "🚫", "escalate": "⚠️"}.get(gate, "🔍")
    lines = [
        f"{emoji} *Dev Analysis — {gate.upper()}*",
        "",
        f"Session: `{session_id_short}` | Score: {result.total_score}",
        (
            f"Findings: {len(result.findings)} "
            f"({n_by_sev['critical']} critical / {n_by_sev['high']} high / "
            f"{n_by_sev['medium']} medium)"
        ),
    ]
    if result.tool_errors:
        lines.append(f"⚠️ Tool errors: {', '.join(result.tool_errors[:2])}")
    if top5:
        lines += ["", "*Top findings:*"]
        for f in top5[:3]:
            lines.append(
                f"• `[{f['severity'].upper()}]` {f['file']}:{f['line']} "
                f"— {f['message'][:60]}"
            )
    lines += [
        "",
        f"✅ Approve: `approve dev fix {session_id_short}`",
        f"❌ Reject: `reject dev fix {session_id_short}`",
    ]
    return "\n".join(lines)


def _generate_cc_runsheet(
    findings: list,
    suggested_fixes: list,
    session_id_short: str,
) -> dict:
    """
    Generate a structured CC runsheet from Phase 1 findings + Phase 2 suggested fixes.

    Groups findings by file. Phase 2 suggested_fixes (keyed by file+line) take
    precedence over the default hint from _default_fix_hint().

    Returns:
      summary — one-line summary for Telegram and prospective content
      files   — [{file, findings: [{line, severity, rule_id, message, suggested_fix}]}]
      acceptance_criteria — list[str]
      soul_constraints    — list[str]
      raw_text            — formatted text the Director pastes to Claude Code
    """
    from dev_harness.analyser import SEVERITY_WEIGHTS

    # Build a lookup from Phase 2 suggested_fixes for O(1) access
    fix_lookup: dict[tuple, str] = {}
    for sf in (suggested_fixes or []):
        key = (sf.get("file", ""), int(sf.get("line", 0)))
        if sf.get("fix"):
            fix_lookup[key] = sf["fix"]

    # Group by file, sort within file by severity weight desc
    by_file: dict[str, list] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)

    file_entries = []
    for file_path in sorted(by_file):
        sorted_findings = sorted(
            by_file[file_path],
            key=lambda f: SEVERITY_WEIGHTS.get(f.severity, 1),
            reverse=True,
        )
        file_entry = {
            "file": file_path,
            "findings": [
                {
                    "line":           f.line,
                    "severity":       f.severity,
                    "rule_id":        f.rule_id,
                    "message":        f.message,
                    "suggested_fix":  fix_lookup.get((f.file, f.line)) or _default_fix_hint(f),
                }
                for f in sorted_findings
            ],
        }
        file_entries.append(file_entry)

    # Acceptance criteria
    has_boundary = any(f.rule_id.startswith("B") for f in findings)
    acceptance = [
        "All pylint findings at warning+ severity resolved or suppressed with justification",
        "Semgrep findings resolved or documented as false-positive",
    ]
    if has_boundary:
        acceptance.append(
            f"boundary_scanner.py reports 0 findings — "
            f"verify with: verify dev fix {session_id_short}"
        )
    else:
        acceptance.append(
            f"Re-run Phase 1 via: verify dev fix {session_id_short} — gate must be APPROVE"
        )

    # Soul constraints (always present — non-negotiable)
    soul_constraints = [
        "B1: governance/ and execution/adapters/ must not invoke an LLM",
        "B2: gate/validate functions must remain deterministic — no call_llm() calls",
        "B3: translator_pass() must always receive a typed result envelope dict",
        "B4: only execution/adapters/qdrant.py may write to restricted Qdrant collections "
            "(semantic, associative, relational, meta)",
        "Gate decision (gate() in analyser.py) is final — LLM output must never alter it",
    ]

    # Raw text for Director to paste to Claude Code
    now_iso = datetime.now(timezone.utc).isoformat()
    raw_lines = [
        f"# Dev-Harness CC Runsheet — Session {session_id_short}",
        f"# Generated: {now_iso}",
        f"# Total findings: {len(findings)} across {len(by_file)} file(s)",
        "",
    ]
    for fe in file_entries:
        raw_lines.append(f"## {fe['file']}")
        for fn in fe["findings"]:
            raw_lines.append(
                f"  Line {fn['line']} [{fn['severity'].upper()}] "
                f"({fn['rule_id']}): {fn['message'][:100]}"
            )
            if fn["suggested_fix"]:
                raw_lines.append(f"  → Fix: {fn['suggested_fix']}")
        raw_lines.append("")

    raw_lines.append("## Acceptance Criteria")
    for ac in acceptance:
        raw_lines.append(f"- {ac}")
    raw_lines.append("")
    raw_lines.append("## Soul Constraints (do not violate)")
    for sc in soul_constraints:
        raw_lines.append(f"- {sc}")

    n_files  = len(by_file)
    summary  = (
        f"Fix {len(findings)} finding(s) across {n_files} file(s). "
        + ("Boundary violations present — B-rules apply. " if has_boundary else "")
        + f"Re-verify with: verify dev fix {session_id_short}"
    )

    return {
        "summary":             summary,
        "files":               file_entries,
        "acceptance_criteria": acceptance,
        "soul_constraints":    soul_constraints,
        "raw_text":            "\n".join(raw_lines),
    }


def _default_fix_hint(f) -> str:
    """Minimal fix hint based on rule_id. Phase 2 classifier provides more specific hints."""
    if f.rule_id == "B1":
        return "Remove LLM call from forbidden zone. Move invocation to cognition layer."
    if f.rule_id == "B2":
        return "Remove call_llm() from gate/validate function. Extract to caller before gate."
    if f.rule_id == "B3":
        return "Replace string literal with typed result envelope: {'result_for_translator': {...}}."
    if f.rule_id == "B4":
        return (
            "Move archive_client write to execution/adapters/qdrant.py. "
            "Specialist agents are read-only on restricted collections."
        )
    if f.rule_id == "TOOLCHAIN_DISABLED":
        return (
            "docker compose build docker-broker "
            "&& docker compose up -d docker-broker"
        )
    if f.type == "lint":
        return f"Resolve pylint issue: {f.message[:60]}"
    if f.type == "security":
        return f"Review semgrep finding: {f.rule_id}"
    return ""
