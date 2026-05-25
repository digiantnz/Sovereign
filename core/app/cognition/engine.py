import json
import logging
import os
import re as _re_fab
import time as _time
from adapters.ollama import OllamaAdapter
from adapters.grok import GrokAdapter
from adapters.claude import ClaudeAdapter
from adapters.gemini import GeminiAdapter
from adapters.groq_inference import GroqInferenceAdapter
from adapters.ollama_cloud import OllamaCloudAdapter
from adapters.openrouter import OpenRouterAdapter
from cognition import prompts
from cognition.dcl import DisclosureControlLayer

from config import cfg as _cfg

MODEL = _cfg.models.primary_inference_model
PERSONAS_DIR = _cfg.paths.personas_dir
SECURITY_AGENT_PATH = os.path.join(PERSONAS_DIR, "SECURITY_AGENT.md")

# ── Memory-first routing (shadow mode) ───────────────────────────────────────
# When True: run semantic intent search in parallel with LLM PASS 1 and log
# agreement/disagreement to episodic memory.  Routing itself is unchanged —
# the LLM result is always used until Reasoning Sunday validates thresholds.
# Flip to False to disable shadow entirely without touching routing logic.
MEMORY_ROUTING_SHADOW_MODE = _cfg.cognitive_loop.memory_routing_shadow_mode
MEMORY_ROUTING_THRESHOLD   = 0.85   # score at which shadow would have overridden LLM

TRANSLATOR_PATH = os.path.join(PERSONAS_DIR, "translator.md")
MAX_TELEGRAM_CHARS = 12000  # Gateway splits at 4000 chars — allow longer responses

# ---------------------------------------------------------------------------
# Translator output sanitiser — deterministic post-processing (no LLM calls)
#
# Phrases that indicate the model leaked its internal reasoning or the raw
# result_for_translator schema into the Director-facing output.  Any sentence
# or bullet that contains one of these strings is stripped before delivery.
#
# NOTE: This tuple is the authoritative list.  The boundary scanner B5 rule
# flags any OTHER string literal in the translator output path that contains
# these phrases — so this definition line is explicitly excluded from B5 scope.
# ---------------------------------------------------------------------------
_TRANSLATOR_LEAK_PHRASES: tuple[str, ...] = (
    "I followed the rules",
    "Led with the answer",
    "per the instructions",
    "as instructed",
    "translation rules",
)

_SENTENCE_SPLIT_RE = _re_fab.compile(r'(?<=[.!?])\s+')
_BULLET_LINE_RE    = _re_fab.compile(r'^\s*(?:[*\-•+]|\d+[.):])\s+')

AGENT_FILE_MAP = {
    # Current names (used in classify prompt)
    "devops_agent":   "devops_agent.md",
    "research_agent": "research_agent.md",
    "business_agent": "business_agent.md",
    "memory_agent":   "memory_agent.md",
    # Legacy aliases — kept for backward compat during transition
    "docker_agent":   "devops_agent.md",
}


logger = logging.getLogger(__name__)


def _translator_sanitise(text: str) -> tuple[str, int, int]:
    """Strip sentences/bullets containing translator meta-commentary leak phrases.

    Deterministic — no LLM calls.

    Returns:
        (cleaned_text, units_stripped, total_units)

    Algorithm:
    - Split on newlines first to respect bullet/paragraph structure.
    - Within each non-bullet paragraph, further split on sentence boundaries.
    - A unit is stripped if it contains any _TRANSLATOR_LEAK_PHRASES entry
      (case-insensitive match).
    - Blank lines between paragraphs are preserved as structural separators.
    - Collapsed consecutive blank lines after stripping to single blank line.
    """
    lines = text.splitlines()
    units: list[tuple[str, str]] = []  # (raw_unit, paragraph_key)

    # First pass: collect all units (bullets stay as lines; prose is
    # sentence-split within its paragraph block)
    para_buf: list[str] = []

    def _flush_para() -> None:
        if not para_buf:
            return
        para_text = " ".join(para_buf)
        sentences = _SENTENCE_SPLIT_RE.split(para_text)
        for s in sentences:
            s = s.strip()
            if s:
                units.append((s, "prose"))
        para_buf.clear()

    for line in lines:
        if not line.strip():
            _flush_para()
            units.append(("", "blank"))
        elif _BULLET_LINE_RE.match(line):
            _flush_para()
            units.append((line.rstrip(), "bullet"))
        else:
            para_buf.append(line.strip())
    _flush_para()

    # Second pass: strip leak phrases
    kept: list[tuple[str, str]] = []
    stripped_count = 0
    total_count = sum(1 for u, k in units if k != "blank")

    for unit_text, kind in units:
        if kind == "blank":
            kept.append((unit_text, kind))
            continue
        lower = unit_text.lower()
        if any(phrase.lower() in lower for phrase in _TRANSLATOR_LEAK_PHRASES):
            stripped_count += 1
            logger.warning(
                "translator_sanitise: stripped leak unit: %r", unit_text[:120]
            )
        else:
            kept.append((unit_text, kind))

    # Third pass: reassemble — collapse consecutive blanks to one
    out_lines: list[str] = []
    last_was_blank = False
    for unit_text, kind in kept:
        if kind == "blank":
            if not last_was_blank and out_lines:
                out_lines.append("")
            last_was_blank = True
        else:
            out_lines.append(unit_text)
            last_was_blank = False

    return "\n".join(out_lines).strip(), stripped_count, total_count


class CognitionEngine:
    def __init__(self, qdrant=None, ledger=None, inference_queue=None):
        self.ollama       = OllamaAdapter()
        self.grok         = GrokAdapter()
        self.claude       = ClaudeAdapter()
        self.gemini       = GeminiAdapter()
        self.groq_inf     = GroqInferenceAdapter()
        self.ollama_cloud = OllamaCloudAdapter()
        self.openrouter   = OpenRouterAdapter()
        self.dcl          = DisclosureControlLayer()
        self.qdrant       = qdrant
        self.ledger       = ledger   # AuditLedger — injected from main lifespan
        self._queue       = inference_queue
        self._had_queue_wait = False
        self._security_persona: str = self._load_security_persona()
        self._provider_registry: dict = self._load_provider_registry()
        self._session_flags: dict = {}
        self._provider_rate_limited: dict[str, float] = {}

    def _load_security_persona(self) -> str:
        if os.path.exists(SECURITY_AGENT_PATH):
            with open(SECURITY_AGENT_PATH) as f:
                return f.read()
        return "You are a security evaluator. Return JSON only."

    def _load_provider_registry(self) -> dict:
        """Load provider_registry.providers from governance.json once at startup.
        Returns empty dict on any error — callers fall back to complexity scorer silently."""
        gov_path = "/app/governance/governance.json"
        try:
            with open(gov_path) as f:
                gov = json.load(f)
            return gov.get("provider_registry", {}).get("providers", {})
        except Exception as e:
            logging.getLogger(__name__).warning(
                "CognitionEngine: provider_registry load failed (%s) — falling back to complexity scorer", e
            )
            return {}

    # ── Session flags ────────────────────────────────────────────────────
    def set_session_flag(self, key: str, value) -> None:
        self._session_flags[key] = value

    def get_session_flag(self, key: str, default=None):
        return self._session_flags.get(key, default)

    def mark_provider_rate_limited(self, provider: str) -> None:
        self._provider_rate_limited[provider] = _time.monotonic()
        logging.getLogger(__name__).warning(
            "provider_rate_limited: %s — skipping for %.0fs", provider, self._PROVIDER_RATE_LIMIT_TTL_S
        )

    # ── Persona loading ──────────────────────────────────────────────────
    def load_persona(self, name: str) -> str:
        filename = AGENT_FILE_MAP.get(name, f"{name.upper()}.md")
        path = os.path.join(PERSONAS_DIR, filename)
        with open(path) as f:
            return f.read()

    def load_orchestrator(self) -> str:
        """Load orchestrator persona (classify / evaluate / memory-decision passes)."""
        path = os.path.join(PERSONAS_DIR, "orchestrator.md")
        with open(path) as f:
            return f.read()

    def load_translator(self) -> str:
        """Load translator persona (Director-facing translation pass only)."""
        if os.path.exists(TRANSLATOR_PATH):
            with open(TRANSLATOR_PATH) as f:
                return f.read()
        return "You are the Director interface. Translate results to plain English. No JSON. No jargon."

    # Domain keyword sets that signal a canonical key lookup is worthwhile.
    # Keys are the domain slug used in MIP key prefixes; values are the trigger phrases.
    _CANONICAL_DOMAIN_SIGNALS: dict[str, frozenset[str]] = {
        "wallet": frozenset({
            "eth", "ethereum", "address", "wallet", "safe", "multisig", "btc",
            "bitcoin", "xpub", "crypto",
        }),
        "networking": frozenset({
            "tailscale", "vpn", "node04", "nextcloud", "url", "http", "https",
            "hostname", "ip address", "ip", "domain",
        }),
        "infrastructure": frozenset({
            "ollama", "qdrant", "endpoint", "port", "6333", "11434",
        }),
        "governance": frozenset({
            "tier", "confirmation", "governance", "soul", "ed25519", "signing",
            "checksum", "low tier", "mid tier", "high tier",
        }),
    }

    async def _load_canonical_context(self, query: str) -> str:
        """MIP canonical key lookup for PASS 1 context.

        Runs before search_all_weighted when the user input touches known key-mapped
        domains.  Calls list_all_keys() to get the live directory, filters to keys
        whose domain segment matches the triggered domains, then calls retrieve_by_key()
        for each match (cap: 4 keys).

        Returns a labelled block of confirmed facts, or "" if nothing matches.
        Results are injected into PASS 1 context separately from vector results so
        the LLM can distinguish deterministic key lookups from similarity results.
        """
        if not self.qdrant:
            return ""
        try:
            q_lower = query.lower()
            q_words = set(_re_fab.split(r"\W+", q_lower))

            # Determine which domains are signalled by the query
            triggered_domains: set[str] = set()
            for domain, signals in self._CANONICAL_DOMAIN_SIGNALS.items():
                if q_words & signals:
                    triggered_domains.add(domain)

            # Also trigger on bare URL presence
            if _re_fab.search(r"https?://", query):
                triggered_domains.add("networking")

            if not triggered_domains:
                return ""

            # Get live key directory (pure payload scroll — no embedding)
            directory = await self.qdrant.list_all_keys()

            # Filter to keys whose domain segment matches a triggered domain
            # Key format: {type}:{domain}:{slug} — domain is the second segment
            matched_keys: list[str] = []
            for entry in directory:
                key = entry.get("key") or ""
                if not key or key == "NO_KEY":
                    continue
                parts = key.split(":")
                if len(parts) >= 2 and parts[1] in triggered_domains:
                    matched_keys.append(key)

            if not matched_keys:
                return ""

            # Retrieve up to 4 matched keys (most specific first — longer slug = more specific)
            matched_keys.sort(key=lambda k: len(k), reverse=True)
            lines: list[str] = []
            for key in matched_keys[:10]:
                entry = await self.qdrant.retrieve_by_key(key)
                if entry:
                    content = entry.get("content", "")
                    title = entry.get("title", "")
                    lines.append(f"- [CANONICAL|{key}] {title}: {content}")

            if not lines:
                return ""
            return "Confirmed facts (exact key lookup — high confidence):\n" + "\n".join(lines)

        except Exception:
            return ""

    async def load_memory_context(self, query: str,
                                   query_type: str = "knowledge") -> tuple[str, float, list[str]]:
        """Search all 7 sovereign collections with context-aware weighting.
        query_type: action | knowledge | session_start — controls collection score weights.
        On very low confidence (< 0.5), ensures a gap entry exists in meta.

        Prepends canonical key lookups (MIP) when the query touches known key-mapped
        domains — wallet, networking, infrastructure, governance.  These are labelled
        as high-confidence confirmed facts, separate from vector similarity results.
        """
        try:
            # MIP canonical lookup — runs before vector search; empty string if no match
            canonical = await self._load_canonical_context(query)

            results = await self.qdrant.search_all_weighted(
                query, query_type=query_type, top_k=3
            )
            confidence = self.qdrant.compute_confidence(results)
            gaps = self.qdrant.get_gaps(results)

            # Priority 5: very low confidence → ensure meta gap entry exists
            if confidence < 0.5:
                await self.qdrant.ensure_gap_entry(query)

            sim_block = ""
            if results:
                lines = [
                    f"- [{r.get('_collection', '?')}|{r.get('timestamp', '')[:10]}|"
                    f"{r['score']:.2f}(w={r.get('_weight', 1.0):.1f})] {r.get('content', '')}"
                    for r in results
                ]
                sim_block = "Relevant memories:\n" + "\n".join(lines)

            # Canonical facts prepended — LLM sees confirmed facts before similarity results
            if canonical and sim_block:
                context = canonical + "\n\n" + sim_block
            elif canonical:
                context = canonical
            else:
                context = sim_block

            # Bump confidence to 1.0 when canonical facts were found — they are exact matches
            if canonical:
                confidence = 1.0

            return context, confidence, gaps
        except Exception:
            return "", 0.0, []

    async def get_due_prospective(self) -> list[dict]:
        """Return prospective items due today or overdue."""
        if not self.qdrant:
            return []
        return await self.qdrant.get_due_prospective()

    # ── LLM routing helpers (queue-aware) ────────────────────────────────
    async def _llm_generate(self, prompt: str, model: str = MODEL,
                            fmt: "str | None" = None,
                            priority: "int | None" = None,
                            timeout: float = 200.0,
                            capture_thinking: bool = False) -> dict:
        if self._queue is not None:
            from adapters.inference_queue import InferenceQueue
            p = priority if priority is not None else InferenceQueue.HIGH
            result = await self._queue.generate(
                prompt, model=model, fmt=fmt, priority=p, timeout=timeout,
                capture_thinking=capture_thinking,
            )
            if result.get("_queue_waited"):
                self._had_queue_wait = True
            return result
        return await self.ollama.generate(prompt, model=model, fmt=fmt,
                                          capture_thinking=capture_thinking)

    async def _llm_chat(self, messages: "list[dict]", model: str = MODEL,
                        fmt: "str | None" = None,
                        priority: "int | None" = None,
                        timeout: float = 200.0) -> dict:
        if self._queue is not None:
            from adapters.inference_queue import InferenceQueue
            p = priority if priority is not None else InferenceQueue.HIGH
            result = await self._queue.chat(
                messages, model=model, fmt=fmt, priority=p, timeout=timeout
            )
            if result.get("_queue_waited"):
                self._had_queue_wait = True
            return result
        return await self.ollama.chat(messages, model=model, fmt=fmt)

    # ── JSON-enforced LLM call with one retry ────────────────────────────
    async def call_llm_json(self, prompt: str, priority: "int | None" = None) -> dict:
        import re as _re_json
        for attempt in range(2):
            # No fmt="json" — Qwen3 grammar-constrained mode returns empty responses.
            # Rely on prompt instruction + extraction fallback instead.
            result = await self._llm_generate("/no_think\n" + prompt, model=MODEL, priority=priority)
            if result.get("status") == "llm_timeout":
                raise ValueError("LLM timed out during JSON call")
            raw = result.get("response", "").strip()
            if not raw:
                if attempt == 0:
                    continue
                raise ValueError("LLM returned empty response after retry")
            cleaned = _re_json.sub(r'```(?:json)?\s*', '', raw).replace('```', '').strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                m = _re_json.search(r'\{.*\}', cleaned, _re_json.DOTALL)
                if m:
                    try:
                        return json.loads(m.group())
                    except json.JSONDecodeError:
                        pass
                if attempt == 0:
                    continue
                raise ValueError(f"LLM returned invalid JSON after retry: {raw[:200]}")

    # ── Universal LLM output parser ───────────────────────────────────────
    def _parse_llm_output(self, raw: str, required: list[str], defaults: dict) -> dict:
        """Robust JSON parser for LLM pass outputs.

        Steps: strip markdown fences → json.loads → regex {…} extraction → defaults.
        Validates each required key and fills missing ones from defaults.
        Logs audit events on schema failure and missing fields.
        Never raises — always returns a usable dict.
        """
        import re as _re_parse
        cleaned = _re_parse.sub(r'```(?:json)?\s*', '', raw).replace('```', '').strip()
        result = None
        try:
            result = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            pass
        if result is None:
            m = _re_parse.search(r'\{.*\}', cleaned, _re_parse.DOTALL)
            if m:
                try:
                    result = json.loads(m.group())
                except (json.JSONDecodeError, ValueError):
                    pass
        if result is None:
            logger.warning("_parse_llm_output: schema failure — raw=%s", raw[:200])
            if self.ledger:
                self.ledger.append("llm_schema_failure", "cognition",
                                   {"raw_preview": raw[:200]})
            return dict(defaults)
        for key in required:
            if result.get(key) is None:
                if key in defaults:
                    logger.warning("_parse_llm_output: missing field %r — filling from defaults", key)
                    if self.ledger:
                        self.ledger.append("llm_field_missing", "cognition", {"field": key})
                    result[key] = defaults[key]
        return result

    # ── Routing history for PASS 1 ───────────────────────────────────────
    async def load_routing_history(self, intent: str) -> str:
        """Search episodic memory for prior routing decisions matching this intent.
        Returns formatted context string for injection into PASS 1 prompt.
        """
        if not self.qdrant:
            return ""
        try:
            results = await self.qdrant.search_all_weighted(
                f"routing_decision {intent}", query_type="action", top_k=3
            )
            hits = [r for r in results
                    if r.get("score", 0) > 0.5
                    and "routing_decision" in r.get("content", "")]
            if not hits:
                return ""
            lines = [f"- [{h.get('_collection','?')}|{h.get('timestamp','')[:10]}] {h.get('content','')}"
                     for h in hits[:3]]
            return "Prior routing decisions for similar intents:\n" + "\n".join(lines)
        except Exception:
            return ""

    # ── Pass 1: CEO Classification ────────────────────────────────────────
    async def ceo_classify(self, user_input: str, context_window=None,
                           cognitive_context: str = "",
                           sovereign_context: str = "") -> dict:
        from execution.adapters.qdrant import classify_query_type
        query_type = classify_query_type(user_input)
        context, confidence, gaps = await self.load_memory_context(
            user_input, query_type=query_type
        )
        if gaps:
            context = context + f"\n\nKnown knowledge gaps: {', '.join(gaps)}"
        prompt = prompts.classify(
            ceo_persona=self.load_orchestrator(),
            user_input=user_input,
            memory_context=context,
            context_window=context_window,
            cognitive_context=cognitive_context,
            sovereign_context=sovereign_context,
        )
        _raw1 = await self._llm_generate(prompt, model=MODEL, capture_thinking=True)
        result = self._parse_llm_output(
            _raw1.get("response", ""),
            required=["intent", "delegate_to", "tier"],
            defaults={
                "intent": "query", "delegate_to": "research_agent", "tier": "LOW",
                "preferred_provider": "local", "delegation_reason": "", "expected_output_format": "",
            },
        )
        result["_memory_confidence"] = confidence
        result["_memory_gaps"] = gaps
        _p1_thinking = _raw1.get("thinking", "")
        if _p1_thinking:
            logger.info("[P1 THINK] %s", _p1_thinking[:2000])
            result["_p1_thinking"] = _p1_thinking
        return result

    async def _memory_route_shadow(self, user_input: str) -> dict:
        """Shadow semantic search for PASS 1 routing validation.

        Embeds user_input and queries the SEMANTIC collection restricted to
        intent_seed entries (source="intent_seed") while excluding historical
        entries (status="historical").  Returns the top candidate and its score.

        Never raises — all errors return a safe default so shadow failures never
        affect actual routing.

        Returns:
            matched:          bool — True if any candidate was found
            key:              str  — semantic:intent:{slug} of top candidate
            score:            float
            action:           str  — "{domain}:{operation}:{name}" from payload
            shadow_intent:    str  — intent slug extracted from key
            above_threshold:  bool — score >= MEMORY_ROUTING_THRESHOLD
        """
        _empty: dict = {
            "matched": False, "key": "", "score": 0.0,
            "action": "", "shadow_intent": "", "above_threshold": False,
        }
        if not self.qdrant:
            return _empty
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            from execution.adapters.qdrant import SEMANTIC
            vector = await self.qdrant._embed(user_input)
            resp = await self.qdrant.archive_client.query_points(
                collection_name=SEMANTIC,
                query=vector,
                query_filter=Filter(
                    must=[FieldCondition(key="source", match=MatchValue(value="intent_seed"))],
                    must_not=[FieldCondition(key="status", match=MatchValue(value="historical"))],
                ),
                limit=1,
                score_threshold=0.0,   # capture top hit regardless of score
                with_payload=True,
            )
            if not resp.points:
                return _empty
            hit = resp.points[0]
            score = hit.score
            payload = hit.payload or {}
            key = payload.get("_key", "")
            action = payload.get("action", "")
            shadow_intent = key[len("semantic:intent:"):] if key.startswith("semantic:intent:") else ""
            return {
                "matched":          True,
                "key":              key,
                "score":            round(score, 4),
                "action":           action,
                "shadow_intent":    shadow_intent,
                "above_threshold":  score >= MEMORY_ROUTING_THRESHOLD,
            }
        except Exception as exc:
            logger.debug("_memory_route_shadow: failed (non-fatal): %s", exc)
            return _empty

    async def _write_shadow_routing_episodic(
        self,
        user_input: str,
        llm_intent: str,
        shadow: dict,
        agreement: bool | None,
    ) -> None:
        """Write one shadow routing observation to episodic memory.

        Fires via asyncio.create_task() — non-blocking, never raises.
        These entries are the dataset for Reasoning Sunday threshold calibration.
        Keyed episodic:shadow_routing:{date}:{llm_intent} — not unique per call
        (LLM-derived title will differ), but the content carries all fields needed.
        """
        if not self.qdrant:
            return
        try:
            from datetime import datetime, timezone
            ts  = datetime.now(timezone.utc).isoformat()
            day = ts[:10]
            content = (
                f"SHADOW_ROUTING [{day}]: "
                f"llm={llm_intent!r} shadow={shadow['shadow_intent']!r} "
                f"score={shadow['score']:.4f} threshold={MEMORY_ROUTING_THRESHOLD} "
                f"above_threshold={shadow['above_threshold']} agreement={agreement}"
            )
            await self.qdrant.store(
                content=content,
                metadata={
                    "type":              "episodic",
                    "domain":            "shadow_routing",
                    "_key":              f"episodic:shadow_routing:{day}:{llm_intent}",
                    "llm_intent":        llm_intent,
                    "shadow_intent":     shadow["shadow_intent"],
                    "shadow_score":      shadow["score"],
                    "shadow_key":        shadow["key"],
                    "above_threshold":   shadow["above_threshold"],
                    "agreement":         agreement,
                    "timestamp":         ts,
                    "outcome":           "shadow_observation",
                },
                collection="episodic",
                writer="sovereign-core",
            )
        except Exception as exc:
            logger.debug("_write_shadow_routing_episodic: failed (non-fatal): %s", exc)

    async def orchestrator_classify(self, user_input: str, context_window=None,
                                     cognitive_context: str = "",
                                     sovereign_context: str = "") -> dict:
        """PASS 1: Orchestrator classification with routing memory lookup.
        New contract: adds specialist + routing_rationale fields.
        Backward compat: also sets delegate_to = specialist.

        Stamps _routing_source: "llm_pass1" on all results.
        When MEMORY_ROUTING_SHADOW_MODE is True, also runs _memory_route_shadow()
        and stamps shadow fields for Reasoning Sunday calibration. Actual routing
        is unchanged — LLM result is always used until thresholds are validated.
        """
        result = await self.ceo_classify(
            user_input, context_window=context_window,
            cognitive_context=cognitive_context, sovereign_context=sovereign_context,
        )
        # Normalise: ensure both specialist and delegate_to are present
        agent = result.get("specialist") or result.get("delegate_to", "")
        result["specialist"]    = agent
        result["delegate_to"]   = agent
        result["_routing_source"] = "llm_pass1"

        if MEMORY_ROUTING_SHADOW_MODE and self.qdrant:
            shadow    = await self._memory_route_shadow(user_input)
            llm_intent = result.get("intent", "")
            agreement  = (shadow["shadow_intent"] == llm_intent) if shadow["matched"] else None

            result["_memory_routing_confidence"]    = shadow["score"]
            result["_memory_routing_key"]           = shadow["key"]
            result["_memory_routing_shadow_intent"] = shadow["shadow_intent"]
            result["_memory_routing_above_threshold"] = shadow["above_threshold"]

            logger.info(
                "SHADOW ROUTING: llm=%r shadow=%r score=%.4f above_threshold=%s agreement=%s",
                llm_intent, shadow["shadow_intent"], shadow["score"],
                shadow["above_threshold"], agreement,
            )

            if shadow["matched"]:
                import asyncio as _asyncio
                _asyncio.create_task(self._write_shadow_routing_episodic(
                    user_input=user_input,
                    llm_intent=llm_intent,
                    shadow=shadow,
                    agreement=agreement,
                ))

        return result

    # ── Pass 2: Specialist Reasoning ──────────────────────────────────────
    async def specialist_reason(self, agent_name: str, delegation: dict, user_input: str) -> dict:
        from skills.loader import SkillLoader
        import re as _re_json
        _log = __import__("logging").getLogger(__name__)
        persona = self.load_persona(agent_name)
        try:
            loader = SkillLoader(agent_name, ledger=self.ledger)
            persona = loader.inject_into_persona(persona)
            if loader.skills:
                _log.debug("SkillLoader: injected %s into %s persona",
                           loader.get_skill_names(), agent_name)
        except Exception as e:
            _log.warning("SkillLoader: failed for %s: %s", agent_name, e)

        prompt = prompts.specialist(
            agent_persona=persona,
            delegation=delegation,
            user_input=user_input,
        )

        # ── PASS 2 routing: external for complex/explicit requests ──────────
        # PASS 1/3/4 always stay local; PASS 2 is the only externally-routed pass.
        # Governance (PASS 3) and classification (PASS 1) must remain deterministic.
        decision = self._routing_decision(prompt, user_input=user_input)

        if decision["use_external"]:
            _log.info(
                "specialist_reason[%s]: routing to %s (reason=%s score=%.3f→%.3f)",
                agent_name, decision["provider"], decision["reason"],
                decision["score"], decision["penalised_score"],
            )
            try:
                _ask_fn = {
                    "claude":       self.ask_claude,
                    "grok":         self.ask_grok,
                    "gemini":       self.ask_gemini,
                    "groq_inference": self.ask_groq_inf,
                    "ollama_cloud": self.ask_ollama_cloud,
                    "openrouter":   self.ask_openrouter,
                }.get(decision["provider"], self.ask_grok)
                raw = await _ask_fn(prompt, agent=agent_name)

                response_text = raw.get("response", "")
                # External providers return freeform text; may wrap JSON in ```json...```
                # Strip markdown code fences first, then extract the outermost {...} block.
                stripped = _re_json.sub(r"```(?:json)?\s*", "", response_text).replace("```", "")
                m = _re_json.search(r"\{.*\}", stripped, _re_json.DOTALL)
                if m:
                    try:
                        result = json.loads(m.group())
                        result["_routed_external"] = True
                        result["_provider"]        = decision["provider"]
                        result["_complexity_score"] = decision["score"]
                        result["_routing_reason"]  = decision["reason"]
                        return result
                    except json.JSONDecodeError:
                        pass
                _log.warning("specialist_reason[%s]: external response had no parseable JSON, falling back to local",
                             agent_name)
            except Exception as exc:
                _log.warning("specialist_reason[%s]: external call failed (%s), falling back to local",
                             agent_name, exc)

        # Local path (default, or fallback from failed external)
        result = await self.call_llm_json(prompt)
        result["_routed_external"]  = False
        result["_provider"]         = "ollama"
        result["_routing_reason"]   = decision["reason"]
        result["_complexity_score"] = decision["score"]
        result["_intended_provider"] = decision["provider"] if decision["use_external"] else "ollama"
        return result

    # ── Pass 3 outbound: Specialist selects skill and builds payload ──────
    async def specialist_outbound(self, agent_name: str, delegation: dict, user_input: str,
                                  context_window=None, sovereign_context: str = "") -> dict:
        """PASS 3 outbound: specialist plans execution — skill, operation, payload.

        Externally routable (same routing logic as specialist_reason).
        Output is a flat dict compatible with _dispatch_inner() (payload fields at top level).
        """
        import re as _re_json
        _log = __import__("logging").getLogger(__name__)
        persona = self.load_persona(agent_name)
        try:
            from skills.loader import SkillLoader
            loader = SkillLoader(agent_name, ledger=self.ledger)
            persona = loader.inject_into_persona(persona)
        except Exception as e:
            _log.warning("SkillLoader[outbound/%s]: %s", agent_name, e)

        routing_history = await self.load_routing_history(delegation.get("intent", ""))
        prompt = prompts.specialist_outbound(
            agent_persona=persona,
            delegation=delegation,
            user_input=user_input,
            routing_history=routing_history,
            context_window=context_window,
            sovereign_context=sovereign_context,
        )

        # External routing applies to outbound (research may use any eligible provider)
        _deleg_reason = delegation.get("delegation_reason", "")
        _deleg_fmt    = delegation.get("expected_output_format", "")
        decision = self._routing_decision(
            prompt, user_input=user_input,
            delegation_reason=_deleg_reason,
            expected_output_format=_deleg_fmt,
        )
        if decision["use_external"]:
            _log.info("specialist_outbound[%s]: routing to %s", agent_name, decision["provider"])
            try:
                _ask_fn = {
                    "claude":       self.ask_claude,
                    "grok":         self.ask_grok,
                    "gemini":       self.ask_gemini,
                    "groq_inference": self.ask_groq_inf,
                    "ollama_cloud": self.ask_ollama_cloud,
                    "openrouter":   self.ask_openrouter,
                }.get(decision["provider"], self.ask_grok)
                raw = await _ask_fn(prompt, agent=agent_name, routing_decision=decision)
                response_text = raw.get("response", "")
                stripped = _re_json.sub(r"```(?:json)?\s*", "", response_text).replace("```", "")
                m = _re_json.search(r"\{.*\}", stripped, _re_json.DOTALL)
                if m:
                    try:
                        result = json.loads(m.group())
                        result["mode"] = "outbound"
                        result["_routed_external"] = True
                        result["_provider"] = decision["provider"]
                        return result
                    except json.JSONDecodeError:
                        pass
                _log.warning("specialist_outbound[%s]: external JSON invalid — falling back", agent_name)
            except Exception as exc:
                _log.warning("specialist_outbound[%s]: external failed (%s) — falling back", agent_name, exc)

        _raw3a = await self._llm_generate("/no_think\n" + prompt, model=MODEL)
        result = self._parse_llm_output(
            _raw3a.get("response", ""),
            required=["skill", "operation"],
            defaults={"skill": "", "operation": "query"},
        )
        result.setdefault("mode", "outbound")
        result["_routed_external"] = False
        result["_provider"] = "ollama"
        return result

    # ── Pass 3 inbound: Specialist interprets execution result ────────────
    async def specialist_inbound(self, agent_name: str, delegation: dict,
                                 outbound: dict, execution_result: dict,
                                 sovereign_context: str = "") -> dict:
        """PASS 3 inbound: specialist interprets the adapter result.

        ALWAYS uses local Ollama — never externally routed.
        Input is never the Director's raw message — fabrication prevention.
        """
        persona = self.load_persona(agent_name)
        prompt = prompts.specialist_inbound(
            agent_persona=persona,
            delegation=delegation,
            outbound=outbound,
            execution_result=execution_result,
            sovereign_context=sovereign_context,
        )
        _raw3b = await self._llm_generate("/no_think\n" + prompt, model=MODEL)
        result = self._parse_llm_output(
            _raw3b.get("response", ""),
            required=["success", "outcome"],
            defaults={"success": False, "outcome": "No result available."},
        )
        result.setdefault("mode", "inbound")
        result.setdefault("success", False)
        result.setdefault("outcome", "")
        result.setdefault("anomaly", None)
        result.setdefault("retry_with", None)
        return result

    # ── Pass 4: Orchestrator evaluation (merged evaluate + memory decision) ─
    async def orchestrator_evaluate(self, delegation: dict, specialist_inbound_result: dict) -> dict:
        """PASS 4: Orchestrator evaluates result and decides memory action.

        ALWAYS uses local Ollama — governance must be deterministic and local.
        Merges the old ceo_evaluate() + ceo_memory_decision() into one LLM call.
        """
        prompt = prompts.orchestrator_evaluate(
            orchestrator_persona=self.load_orchestrator(),
            delegation=delegation,
            specialist_inbound_result=specialist_inbound_result,
        )
        _raw4 = await self._llm_generate("/no_think\n" + prompt, model=MODEL)
        result = self._parse_llm_output(
            _raw4.get("response", ""),
            required=["result_for_translator"],
            defaults={"result_for_translator": {
                "success": False, "outcome": "Evaluation failed.",
                "detail": {}, "error": None, "next_action": None,
            }},
        )
        # Validate result_for_translator structure — PASS 4 sometimes emits {} or omits outcome
        _rft_check = result.get("result_for_translator", {})
        if not isinstance(_rft_check, dict) or "outcome" not in _rft_check:
            result["result_for_translator"] = {
                "success": specialist_inbound_result.get("success", False),
                "outcome": specialist_inbound_result.get("outcome", "Research complete."),
                "detail": specialist_inbound_result.get("detail", {}),
                "error": None,
                "next_action": None,
            }
            if self.ledger:
                self.ledger.append("pass4_rft_fallback", "internal",
                                   {"reason": "invalid_rft_structure"})
        result.setdefault("approved", True)
        result.setdefault("feedback", None)
        result.setdefault("memory_action", "none")
        result.setdefault("memory_payload", None)
        result.setdefault("result_for_translator", {
            "success": specialist_inbound_result.get("success", False),
            "outcome": specialist_inbound_result.get("outcome", ""),
            "detail": {},
            "error": None,
            "next_action": None,
        })
        return result

    # ── Pass 5: Translator (restricted input — result_for_translator only) ─
    # ── Translator fabrication guard ─────────────────────────────────────
    @staticmethod
    def _is_result_empty(rft: dict) -> bool:
        """True when result_for_translator carries no reportable factual data."""
        detail = rft.get("detail") or {}
        has_detail = bool(
            (isinstance(detail, str) and detail.strip())
            or (isinstance(detail, dict) and any(
                v for v in detail.values()
                if v is not None and v != "" and v != [] and v != {}
            ))
        )
        has_outcome  = bool(str(rft.get("outcome",  "")).strip())
        has_response = bool(str(rft.get("response", "")).strip())
        return not (has_detail or has_outcome or has_response)

    @staticmethod
    def _check_fabrication(text: str, rft: dict) -> str | None:
        """Post-translation fabrication check.

        Extracts integers ≥ 4 from translator output and verifies each appears
        in the JSON-serialised result_for_translator.  A number invented by the
        LLM (not present in the result) is a fabrication violation.

        Returns None if clean, or a fallback string if fabrication is detected.
        Numbers 0-3 are ubiquitous (versions, bullets) and skipped to avoid FPs.
        """
        result_text = json.dumps(rft)
        for num in set(_re_fab.findall(r'\b([4-9]\d*|\d{2,})\b', text)):
            if num not in result_text:
                logger.warning(
                    "translator_pass FABRICATION: number '%s' not in result. "
                    "Blocking output. result=%s output=%s",
                    num, result_text[:300], text[:300],
                )
                fallback = (rft.get("outcome") or rft.get("response") or "").strip()
                return fallback if fallback else "I don't have that information in memory or current context."
        return None

    async def translator_pass(self, result_for_translator: dict, tier: str = "LOW") -> str:
        """PASS 5: Translator receives ONLY result_for_translator — nothing else.

        Hard pre-check: empty/failed results bypass the LLM entirely — deterministic
        Python messages are returned immediately, eliminating the hallucination risk.
        Post-check: after LLM translation, verify all numbers in output exist in the
        result.  Any invented number is a fabrication violation → block + fallback.
        """
        _had_wait = self._had_queue_wait
        self._had_queue_wait = False
        success  = result_for_translator.get("success", True)
        has_error = bool(result_for_translator.get("error"))

        # ── Hard pre-check A: failure path — deterministic, no LLM ────────────
        # Exception: awaiting_confirmation is not a failure — it's a confirmation prompt.
        # Pass these through to the LLM so the translator can phrase the ask clearly.
        _is_awaiting = (
            result_for_translator.get("next_action") == "confirm_or_deny"
            or result_for_translator.get("outcome") == "awaiting_confirmation"
        )
        if (has_error or not success) and not _is_awaiting:
            error_detail = (
                result_for_translator.get("error")
                or result_for_translator.get("outcome")
                or result_for_translator.get("message")
                or result_for_translator.get("reason")
                or result_for_translator.get("status")
            )
            if not error_detail:
                # Log the full dict so the failure is diagnosable in container logs
                logger.warning(
                    "translator_pass: failure result with no error/outcome — full dict: %s",
                    json.dumps(result_for_translator)[:2000],
                )
                error_detail = "no error detail returned (check sovereign-core logs)"
            logger.info("translator_pass: failure result — bypassing LLM")
            return f"That action failed: {error_detail}"

        # ── Hard pre-check B: empty result — deterministic, no LLM ───────────
        if self._is_result_empty(result_for_translator):
            logger.warning("translator_pass: empty result_for_translator — bypassing LLM (integrity guard)")
            return "I don't have that information in memory or current context."

        # ── Has data — call translator LLM ────────────────────────────────────
        try:
            prompt = "/no_think\n" + prompts.translate_from_orchestrator(
                translator_persona=self.load_translator(),
                result_for_translator=result_for_translator,
                tier=tier,
            )
            raw = await self._llm_generate(prompt, model=MODEL)
            text = raw.get("response", "").strip()

            # ── Post-check: fabrication detection (numbers) ───────────────────
            fabrication_fallback = self._check_fabrication(text, result_for_translator)
            if fabrication_fallback is not None:
                return fabrication_fallback

            # Strip spurious urgency unless HIGH-tier error
            allow_urgent = tier == "HIGH"  # error already handled above
            if not allow_urgent:
                text = self._URGENT_STRIP_RE.sub("", text).strip()
            # Strip preamble the model adds despite instructions
            # Catches any "Here is/Here's <up to 80 chars>:" opener at start of response
            text = _re_fab.sub(
                r"^(Here(?:'s| is)\b[^:]{0,80}:\s*\n?"
                r"|Translation:\s*|Translated message:\s*|Plain English(?:\s+\w+)?:\s*)",
                "", text, flags=_re_fab.IGNORECASE,
            ).strip()
            # Strip trailing meta-commentary the model appends (system prompt leakage)
            # Catches: "\n\nNote: ...", "\n\n(Note: ...)", "No further action is required.", etc.
            text = _re_fab.sub(
                r"\n*\(?Note:\s+[^\n]{10,}\)?$",
                "", text, flags=_re_fab.IGNORECASE,
            ).strip()
            text = _re_fab.sub(
                r"\n*(No further action (?:is )?required\.?)$",
                "", text, flags=_re_fab.IGNORECASE,
            ).strip()

            # ── Deterministic leak sanitiser ──────────────────────────────
            text, n_stripped, n_total = _translator_sanitise(text)

            if n_total > 0 and n_stripped / n_total > 0.30:
                # >30% stripped → violation: log to episodic and retry once
                logger.warning(
                    "translator_pass: VIOLATION — %d/%d units stripped (%.0f%%). "
                    "Retrying once.",
                    n_stripped, n_total, 100 * n_stripped / n_total,
                )
                import asyncio as _asyncio
                _asyncio.create_task(self._log_translator_violation(
                    result_for_translator, n_stripped, n_total
                ))
                # Retry — same prompt, one more attempt
                try:
                    raw2 = await self._llm_generate(prompt, model=MODEL)
                    text2 = raw2.get("response", "").strip()
                    # Apply all same deterministic strips to retry output
                    if not allow_urgent:
                        text2 = self._URGENT_STRIP_RE.sub("", text2).strip()
                    text2, _, _ = _translator_sanitise(text2)
                    if len(text2) > MAX_TELEGRAM_CHARS:
                        _t2c = text2[:MAX_TELEGRAM_CHARS]
                        _t2s = max(_t2c.rfind('. '), _t2c.rfind('.\n'))
                        text2 = (_t2c[:_t2s + 1] if _t2s > MAX_TELEGRAM_CHARS // 2 else _t2c)
                        text2 += "\n\n[Response truncated — ask for more detail if needed]"
                    if text2:
                        return text2
                except Exception:
                    pass
                # Retry failed or empty — use the already-sanitised first attempt

            if len(text) > MAX_TELEGRAM_CHARS:
                truncated = text[:MAX_TELEGRAM_CHARS]
                last_sentence = max(truncated.rfind('. '), truncated.rfind('.\n'))
                if last_sentence > MAX_TELEGRAM_CHARS // 2:
                    text = truncated[:last_sentence + 1]
                else:
                    text = truncated
                text += "\n\n[Response truncated — ask for more detail if needed]"

            if _had_wait:
                text = "_Your request was queued briefly — the GPU was busy with a background task._\n\n" + text
            return text
        except Exception:
            return result_for_translator.get("detail", {}).get("outcome", "")

    async def _log_translator_violation(
        self, result_for_translator: dict, n_stripped: int, n_total: int
    ) -> None:
        """Write a translator boundary violation to episodic memory (async, non-blocking)."""
        if not self.qdrant:
            return
        from datetime import datetime, timezone
        from execution.adapters.qdrant import EPISODIC
        ts = datetime.now(timezone.utc).isoformat()
        try:
            await self.qdrant.store(
                content=(
                    f"Translator boundary violation at {ts}: "
                    f"{n_stripped}/{n_total} output units contained "
                    "internal reasoning leak phrases and were stripped. "
                    "Percentage exceeded 30% threshold — output retried."
                ),
                metadata={
                    "type": "translator_violation",
                    "event_type": "translator_leak",
                    "n_stripped": n_stripped,
                    "n_total": n_total,
                    "pct_stripped": round(100 * n_stripped / n_total, 1),
                    "ts": ts,
                    "_key": f"episodic:translator_violation:{ts}",
                },
                collection=EPISODIC,
                writer="sovereign-core",
            )
        except Exception as e:
            logger.warning("translator_pass: could not write violation to episodic: %s", e)

    # ── Pass 3: CEO Evaluation ────────────────────────────────────────────
    async def ceo_evaluate(self, user_input: str, delegation: dict, specialist_output: dict) -> dict:
        prompt = prompts.evaluate(
            ceo_persona=self.load_orchestrator(),
            user_input=user_input,
            delegation=delegation,
            specialist_output=specialist_output,
        )
        return await self.call_llm_json(prompt)

    # ── Security Evaluation Pass ──────────────────────────────────────────
    async def security_evaluate(self, scan_result, content: str) -> dict:
        """Evaluate a flagged scan result with the Security Agent persona.
        Returns {"block": bool, "risk_level": str, "risk_categories": [...],
                 "reasoning_summary": str, "required_mitigation": str}"""
        # Build context snippets: for each matched phrase, extract the surrounding line(s)
        # so the LLM can judge whether it's a malicious instruction or legitimate documentation
        # (e.g. "ignore previous instructions" appearing in a security warning section).
        phrase_contexts = []
        content_lower = content.lower()
        for phrase in scan_result.matched_phrases[:5]:
            idx = content_lower.find(phrase.lower())
            if idx >= 0:
                # Grab ~120 chars of context centred on the phrase
                start = max(0, idx - 60)
                end = min(len(content), idx + len(phrase) + 60)
                snippet = content[start:end].replace("\n", " ").strip()
                phrase_contexts.append({"phrase": phrase, "context": snippet})
        prompt = prompts.security_eval(
            security_persona=self._security_persona,
            scan_categories=scan_result.categories,
            matched_phrases=scan_result.matched_phrases,
            content_preview=content[:2000],
            phrase_contexts=phrase_contexts,
        )
        result = await self.call_llm_json(prompt)
        return result

    # ── Pass 4: Memory Decision ───────────────────────────────────────────
    async def ceo_memory_decision(self, user_input: str, execution_result: dict) -> dict:
        prompt = prompts.memory_decision(
            ceo_persona=self.load_orchestrator(),
            user_input=user_input,
            execution_result=execution_result,
        )
        return await self.call_llm_json(prompt)

    # ── CEO Agent translation (Director interface pass) ───────────────────
    # Urgency is deterministic: only allowed when result signals a security block or error.
    # The LLM must not invent urgency for routine informational results.
    _URGENT_STRIP_RE = __import__("re").compile(
        r"^(URGENT|ALERT|WARNING|CRITICAL)[:\s–—\-]+",
        __import__("re").IGNORECASE,
    )

    async def ceo_translate(self, user_input: str, result: dict, tier: str = "LOW") -> str:
        """Translate any Sovereign result into plain Director-facing English.
        tier: the governance tier of the originating action (LOW/MID/HIGH) — used to
        enforce deterministic urgency stripping. Only HIGH-tier errors may retain urgency.
        Returns translated string, or empty string on failure (caller uses fallback)."""
        try:
            prompt = prompts.translate_for_director(
                ceo_agent_persona=self.load_translator(),
                user_input=user_input,
                result=result,
                tier=tier,
            )
            raw = await self._llm_generate(prompt, model=MODEL)
            text = raw.get("response", "").strip()
            # Strip spurious urgency prefix unless this is a HIGH-tier error result.
            # Urgency mapping: LOW = informational, MID = action required, HIGH = time-sensitive.
            # URGENT prefix is only valid for HIGH tier with an actual error/security result.
            has_error = bool(result.get("error") or result.get("status") == "error")
            allow_urgent = tier == "HIGH" and has_error
            if not allow_urgent:
                text = self._URGENT_STRIP_RE.sub("", text).strip()
            # Strip any meta-commentary the model appended despite instructions
            import re as _re2
            # Strip leading preamble the small model adds despite instructions
            text = _re2.sub(
                r"^(Here(?:'s| is) the (?:Director message|translated message|message)[^:]*:\s*"
                r"|Translation:\s*"
                r"|Translated message:\s*"
                r"|Plain English(?:\s+message)?:\s*)",
                "", text, flags=_re2.IGNORECASE,
            ).strip()
            # Strip trailing meta-commentary the model appends
            text = _re2.sub(
                r"\n*(This message meets|Here is the translated|Communication preference|"
                r"Director communication|Please note that|Note:|---+|"
                r"Urgency does not apply|The live adapter result|skills_live|"
                r"The source of this information is).*",
                "", text, flags=_re2.IGNORECASE | _re2.DOTALL
            ).strip()
            return text
        except Exception:
            return ""

    # ── Conversational query ───────────────────────────────────────────────
    async def ask_conversational(self, user_input: str, context_window: dict = None) -> dict:
        context, confidence, _gaps = await self.load_memory_context(user_input)
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _now_utc = _dt.now(_tz.utc)
        _now_nz = _now_utc + _td(hours=12)  # NZST (UTC+12); NZDT is +13 Oct–Apr
        _time_str = _now_nz.strftime("%H:%M NZST on %A %-d %B %Y") + f" ({_now_utc.strftime('%H:%M UTC')})"
        system = (
            f"You are Sovereign, a helpful personal AI assistant. Answer directly and helpfully in plain English.\n"
            f"Current time: {_time_str}\n"
            "CRITICAL: Only reference events, requests, or context that appear explicitly in the conversation history "
            "below. Do NOT invent, assume, or hallucinate prior requests or context that are not shown. "
            "If you are uncertain about prior context, say so honestly rather than guessing."
        )
        # Only inject memory context if it's genuinely relevant — very low scores
        # are noise, but 0.40+ is meaningful for intra-session context recall.
        if context and confidence >= 0.40:
            system += f"\n\nRelevant context:\n{context}"
        messages = [{"role": "system", "content": system}]
        # Inject prior turns so follow-ups like "summarise that" resolve correctly
        if context_window:
            turns = context_window if isinstance(context_window, list) else [context_window]
            for t in turns[-8:]:
                messages.append({"role": "user", "content": t.get("user", "")})
                messages.append({"role": "assistant", "content": t.get("assistant", "")})
        messages.append({"role": "user", "content": user_input})
        return await self._llm_chat(messages, model=MODEL)

    # ── Direct query (existing /query route) ─────────────────────────────
    async def ask_local(self, prompt: str, model: str = MODEL,
                        priority: "int | None" = None,
                        timeout: float = 200.0) -> dict:
        return await self._llm_generate(prompt, model=model, priority=priority, timeout=timeout)

    # ── External cognition — Grok ─────────────────────────────────────────
    async def ask_grok(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
        routing_decision: dict | None = None,
    ) -> dict:
        """DCL-gated Grok call. Returns {response} or {error} if blocked.
        Every call — including blocks — is logged to audit."""
        if routing_decision and (routing_decision.get("delegation_reason") or routing_decision.get("expected_output_format")):
            prompt = (
                f"[SOVEREIGN DELEGATION]\n"
                f"Reason: {routing_decision.get('delegation_reason', '')}\n"
                f"Expected output format: {routing_decision.get('expected_output_format', 'prose')}\n"
                f"---\n{prompt}"
            )
            logger.info("provider_delegation: grok reason=%s", routing_decision.get("delegation_reason", ""))
        dcl_result = self.dcl.prepare(prompt, agent=agent, provider="grok")
        if dcl_result.blocked:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger)
            return {"error": "DCL_BLOCKED", "sensitivity": dcl_result.tier,
                    "message": "Content classified SECRET — not transmitted to external provider."}
        try:
            kwargs = {"prompt": dcl_result.content, "system": system}
            if model:
                kwargs["model"] = model
            raw = await self.grok.generate(**kwargs)
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger,
                                  output_tokens=raw.get("output_tokens", 0))
            return {"response": raw["response"]}
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                self.mark_provider_rate_limited("grok")
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger, provider_error=str(e))
            return {"error": str(e)}

    # ── External cognition — Claude ───────────────────────────────────────
    async def ask_claude(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
    ) -> dict:
        """DCL-gated Claude call. Returns {response} or {error} if blocked.
        Every call — including blocks — is logged to audit."""
        dcl_result = self.dcl.prepare(prompt, agent=agent, provider="claude")
        if dcl_result.blocked:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger)
            return {"error": "DCL_BLOCKED", "sensitivity": dcl_result.tier,
                    "message": "Content classified SECRET — not transmitted to external provider."}
        try:
            kwargs = {"prompt": dcl_result.content, "system": system}
            if model:
                kwargs["model"] = model
            raw = await self.claude.generate(**kwargs)
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger,
                                  output_tokens=raw.get("output_tokens", 0))
            return {"response": raw["response"]}
        except Exception as e:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger, provider_error=str(e))
            return {"error": str(e)}

    # ── External cognition — Gemini ───────────────────────────────────────
    async def ask_gemini(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
        routing_decision: dict | None = None,
    ) -> dict:
        """DCL-gated Gemini call. Returns {response, _trust} or {error} if blocked."""
        if routing_decision and (routing_decision.get("delegation_reason") or routing_decision.get("expected_output_format")):
            prompt = (
                f"[SOVEREIGN DELEGATION]\n"
                f"Reason: {routing_decision.get('delegation_reason', '')}\n"
                f"Expected output format: {routing_decision.get('expected_output_format', 'prose')}\n"
                f"---\n{prompt}"
            )
            logger.info("provider_delegation: gemini reason=%s", routing_decision.get("delegation_reason", ""))
        dcl_result = self.dcl.prepare(prompt, agent=agent, provider="gemini")
        if dcl_result.blocked:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger)
            return {"error": "DCL_BLOCKED", "sensitivity": dcl_result.tier,
                    "message": "Content classified SECRET — not transmitted to external provider."}
        try:
            kwargs = {"prompt": dcl_result.content, "system": system}
            if model:
                kwargs["model"] = model
            raw = await self.gemini.generate(**kwargs)
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger,
                                  output_tokens=raw.get("output_tokens", 0))
            if raw.get("status") == "error" or not raw.get("response"):
                raise ValueError(raw.get("error", "empty response from Gemini"))
            return {"response": raw["response"], "_trust": "untrusted_external"}
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                self.mark_provider_rate_limited("gemini")
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger, provider_error=str(e))
            return {"error": str(e), "_trust": "untrusted_external"}

    # ── External cognition — Groq Inference ──────────────────────────────
    async def ask_groq_inf(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
        routing_decision: dict | None = None,
    ) -> dict:
        """DCL-gated Groq Inference call. Returns {response, _trust} or {error} if blocked."""
        if routing_decision and (routing_decision.get("delegation_reason") or routing_decision.get("expected_output_format")):
            prompt = (
                f"[SOVEREIGN DELEGATION]\n"
                f"Reason: {routing_decision.get('delegation_reason', '')}\n"
                f"Expected output format: {routing_decision.get('expected_output_format', 'prose')}\n"
                f"---\n{prompt}"
            )
            logger.info("provider_delegation: groq_inference reason=%s", routing_decision.get("delegation_reason", ""))
        dcl_result = self.dcl.prepare(prompt, agent=agent, provider="groq_inference")
        if dcl_result.blocked:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger)
            return {"error": "DCL_BLOCKED", "sensitivity": dcl_result.tier,
                    "message": "Content classified SECRET — not transmitted to external provider."}
        try:
            kwargs = {"prompt": dcl_result.content, "system": system}
            if model:
                kwargs["model"] = model
            raw = await self.groq_inf.generate(**kwargs)
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger,
                                  output_tokens=raw.get("output_tokens", 0))
            if raw.get("status") == "error" or not raw.get("response"):
                raise ValueError(raw.get("error", "empty response from Groq"))
            return {"response": raw["response"], "_trust": "untrusted_external"}
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                self.mark_provider_rate_limited("groq_inference")
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger, provider_error=str(e))
            return {"error": str(e), "_trust": "untrusted_external"}

    # ── External cognition — Ollama Cloud ────────────────────────────────
    async def ask_ollama_cloud(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
        routing_decision: dict | None = None,
    ) -> dict:
        """DCL-gated Ollama Cloud call. Returns {response, _trust} or {error} if blocked."""
        if routing_decision and (routing_decision.get("delegation_reason") or routing_decision.get("expected_output_format")):
            prompt = (
                f"[SOVEREIGN DELEGATION]\n"
                f"Reason: {routing_decision.get('delegation_reason', '')}\n"
                f"Expected output format: {routing_decision.get('expected_output_format', 'prose')}\n"
                f"---\n{prompt}"
            )
            logger.info("provider_delegation: ollama_cloud reason=%s", routing_decision.get("delegation_reason", ""))
        dcl_result = self.dcl.prepare(prompt, agent=agent, provider="ollama_cloud")
        if dcl_result.blocked:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger)
            return {"error": "DCL_BLOCKED", "sensitivity": dcl_result.tier,
                    "message": "Content classified SECRET — not transmitted to external provider."}
        try:
            kwargs = {"prompt": dcl_result.content, "system": system}
            if model:
                kwargs["model"] = model
            raw = await self.ollama_cloud.generate(**kwargs)
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger,
                                  output_tokens=raw.get("output_tokens", 0))
            if raw.get("status") == "error" or not raw.get("response"):
                raise ValueError(raw.get("error", "empty response from Ollama Cloud"))
            return {"response": raw["response"], "_trust": "untrusted_external"}
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                self.mark_provider_rate_limited("ollama_cloud")
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger, provider_error=str(e))
            return {"error": str(e), "_trust": "untrusted_external"}

    # ── External cognition — OpenRouter ──────────────────────────────────
    async def ask_openrouter(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
        routing_decision: dict | None = None,
    ) -> dict:
        """DCL-gated OpenRouter call. Returns {response, _trust} or {error} if blocked."""
        if routing_decision and (routing_decision.get("delegation_reason") or routing_decision.get("expected_output_format")):
            prompt = (
                f"[SOVEREIGN DELEGATION]\n"
                f"Reason: {routing_decision.get('delegation_reason', '')}\n"
                f"Expected output format: {routing_decision.get('expected_output_format', 'prose')}\n"
                f"---\n{prompt}"
            )
            logger.info("provider_delegation: openrouter reason=%s", routing_decision.get("delegation_reason", ""))
        dcl_result = self.dcl.prepare(prompt, agent=agent, provider="openrouter")
        if dcl_result.blocked:
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger)
            return {"error": "DCL_BLOCKED", "sensitivity": dcl_result.tier,
                    "message": "Content classified SECRET — not transmitted to external provider."}
        try:
            kwargs = {"prompt": dcl_result.content, "system": system}
            if model:
                kwargs["model"] = model
            raw = await self.openrouter.generate(**kwargs)
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger,
                                  output_tokens=raw.get("output_tokens", 0))
            if raw.get("status") == "error" or not raw.get("response"):
                raise ValueError(raw.get("error", "empty response from OpenRouter"))
            return {"response": raw["response"], "_trust": "untrusted_external"}
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                self.mark_provider_rate_limited("openrouter")
            if self.ledger:
                self.dcl.log_call(dcl_result, self.ledger, provider_error=str(e))
            return {"error": str(e), "_trust": "untrusted_external"}

    # ── Routed cognition (registry-aware, local-first) ───────────────────────
    #
    # ROUTING LOGIC (for Rex's self-diagnostic):
    #
    # Step 1 — DCL sensitivity gate (hard block, checked first):
    #   PRIVATE or SECRET content → local always, no external call ever
    #
    # Step 2 — Explicit provider override in user input:
    #   "use grok/ask grok/via grok"             → grok (if eligible)
    #   "use gemini/ask gemini/via gemini"        → gemini (if eligible)
    #   "use groq/ask groq/via groq"              → groq_inference (if eligible)
    #   "use openrouter/ask openrouter"           → openrouter (if eligible)
    #   "use claude/ask claude/via claude"        → claude (if eligible)
    #
    # Step 3 — task_type specialist match:
    #   alpha_vantage task_types → returns use_external=False, provider="alpha_vantage"
    #   (actual alpha_vantage call happens via execution engine research harness;
    #    the provider tag is for audit logging only — do NOT add alpha_vantage to
    #    the LLM dispatch dict in specialist_reason/specialist_outbound/route_cognition)
    #
    # Step 4 — Default preference by task_type:
    #   web_aware_query / news_gather → grok (web-grounded)
    #   llm_generate / llm_chat       → grok (default) when complexity ≥ 0.50
    #   else → local
    #
    # Step 5 — Complexity fallback (score ≥ 0.50, after operational penalty) → grok
    #
    # Step 6 — Default → Ollama local
    #
    # Provider eligibility gate: enabled=True AND task_type in task_types
    # AND DCL tier in eligible_classifications.  Falls back to complexity scorer
    # silently if provider_registry is empty or lookup fails.
    #
    # ALL external calls are DCL-gated and audit-logged regardless of trigger.
    # PASS 1 (classify), PASS 3 (evaluate), PASS 4 (memory) are NEVER routed
    # externally — governance must remain deterministic and local.

    _GROK_PRIORITY_TASKS       = frozenset({"news_gather", "web_aware_query", "market_sentiment", "cve_monitor"})
    _PROVIDER_QUEUE            = ["groq_inference", "gemini", "openrouter", "ollama_cloud", "grok"]
    _PROVIDER_RATE_LIMIT_TTL_S = 3600.0

    _COMPLEXITY_THRESHOLD = 0.50
    _PREFER_LOCAL_TIERS   = {"PRIVATE", "SECRET"}

    _EXPLICIT_EXTERNAL_RE = __import__("re").compile(
        r"\b(use claude|use grok|use gemini|use groq|use openrouter|use ollama.?cloud"
        r"|ask claude|ask grok|ask gemini|ask groq|ask openrouter"
        r"|via claude|via grok|via gemini|via groq|via openrouter"
        r"|external llm|external model|external ai)\b",
        __import__("re").IGNORECASE,
    )
    # Named-provider explicit signals (checked against raw user input)
    _CLAUDE_SIGNAL_RE = __import__("re").compile(
        r"\b(use claude|ask claude|via claude"
        r"|architectural|architecture|plan|review|design|strategy|strategic)\b",
        __import__("re").IGNORECASE,
    )
    _GROK_SIGNAL_RE = __import__("re").compile(
        r"\b(use grok|ask grok|via grok"
        r"|current|latest|news|today|recent|market|trending)\b",
        __import__("re").IGNORECASE,
    )
    _GEMINI_SIGNAL_RE = __import__("re").compile(
        r"\b(use gemini|ask gemini|via gemini)\b",
        __import__("re").IGNORECASE,
    )
    _GROQ_SIGNAL_RE = __import__("re").compile(
        r"\b(use groq|ask groq|via groq)\b",
        __import__("re").IGNORECASE,
    )
    _OPENROUTER_SIGNAL_RE = __import__("re").compile(
        r"\b(use openrouter|ask openrouter|via openrouter)\b",
        __import__("re").IGNORECASE,
    )
    # Operational/infra keywords that trigger the complexity penalty
    _OPERATIONAL_RE = __import__("re").compile(
        r"\b(restart|container|service|deploy|mount|volume|port|compose|dockerfile"
        r"|nginx|redis|mariadb|healthcheck|network|subnet)\b",
        __import__("re").IGNORECASE,
    )

    # Maps intent-derived signals in user_input to provider_registry task_type names.
    # Default: "llm_generate" for anything not in this map.
    _INTENT_TO_TASK_TYPE = {
        "web search":              "web_aware_query",  # natural language "web search X"
        "research":                "web_aware_query",
        "news":                    "news_gather",    # matches "latest news", "news today", "news brief"
        "securities_price":        "securities_price",
        "securities_fundamentals": "securities_fundamentals",
        "securities_technicals":   "securities_technicals",
        "commodities_price":       "commodities_price",
        "economic_indicators":     "economic_indicators",
    }

    # Default LLM provider preferences by task_type (used when no explicit override)
    _TASK_TYPE_PREFERRED = {
        "web_aware_query": "grok",
        "news_gather":     "grok",
    }

    # Adapter module paths for the `adapter` return field (audit logging)
    _PROVIDER_ADAPTERS = {
        "grok":         "core.app.adapters.grok",
        "claude":       "core.app.adapters.claude",
        "gemini":       "core.app.adapters.gemini",
        "groq_inference": "core.app.adapters.groq_inference",
        "ollama_cloud": "core.app.adapters.ollama_cloud",
        "openrouter":   "core.app.adapters.openrouter",
        "alpha_vantage": "core.app.adapters.alpha_vantage",
        "local":        "local",
    }

    @staticmethod
    def _complexity_score(prompt: str) -> float:
        """Heuristic complexity score in [0, 1]. Higher = more complex.

        Factors (weights sum to 1.0):
          • Length       — >300 words                              (0.40)
          • Conjunctions — and/also/furthermore/moreover/however   (0.20)
          • Depth kw     — analyse/compare/evaluate/trade-offs     (0.25)
          • Question cnt — multiple ? marks                        (0.15)
        Operational penalty applied separately in _routing_decision.
        """
        words      = prompt.split()
        length_s   = min(len(words) / 300, 1.0)
        multi_conj = len(__import__("re").findall(
            r"\b(and|also|additionally|furthermore|moreover|however)\b",
            prompt, __import__("re").IGNORECASE,
        )) / 10
        depth_kw   = len(__import__("re").findall(
            r"\b(analyse|analyze|synthesise|synthesize|compare|contrast"
            r"|evaluate|critique|assess|implications|trade-offs?)\b",
            prompt, __import__("re").IGNORECASE,
        )) / 5
        q_count    = min(prompt.count("?") / 3, 1.0)
        return min(length_s * 0.4 + multi_conj * 0.2 + depth_kw * 0.25 + q_count * 0.15, 1.0)

    def _routing_decision(
        self,
        prompt: str,
        user_input: str = "",
        task_type: str | None = None,
        delegation_reason: str = "",
        expected_output_format: str = "",
    ) -> dict:
        """Registry-aware routing decision. Returns:
          {use_external, provider, adapter, score, penalised_score, explicit, force_local,
           reason, delegation_reason, expected_output_format}

        Consults provider_registry from governance.json (loaded once at startup).
        Falls back silently to complexity scorer if registry is empty or lookup fails.
        user_input is the raw Director message (for explicit-override and signal matching).
        task_type is the execution-layer task type; inferred from user_input if not passed.
        delegation_reason / expected_output_format are threaded into the return dict and
        prepended as a [SOVEREIGN DELEGATION] block by each ask_* wrapper.
        """
        _log = logging.getLogger(__name__)
        signal_text = user_input or prompt

        # ── Step 1: DCL gate (hard block) ─────────────────────────────────
        tier        = self.dcl.classify(prompt)
        force_local = tier in self._PREFER_LOCAL_TIERS
        if force_local:
            return {
                "use_external": False, "provider": "local",
                "adapter": "local",
                "score": 0.0, "penalised_score": 0.0,
                "explicit": False, "force_local": True,
                "reason": "force_local(dcl)",
                "delegation_reason": delegation_reason,
                "expected_output_format": expected_output_format,
            }

        # CONFIDENTIAL gate: if Director has explicitly approved external use for this
        # session, treat CONFIDENTIAL as WORKSPACE_INTERNAL for eligibility checks.
        effective_tier = (
            "WORKSPACE_INTERNAL"
            if tier == "CONFIDENTIAL" and self.get_session_flag("confidential_external_approved")
            else tier
        )

        # ── Complexity scoring (used in fallback and for audit logging) ────
        score     = self._complexity_score(user_input or prompt)
        penalised = score
        if score >= self._COMPLEXITY_THRESHOLD and self._OPERATIONAL_RE.search(prompt):
            penalised = max(0.0, score - 0.20)

        # ── Step 2: Explicit named-provider override ───────────────────────
        explicit = bool(self._EXPLICIT_EXTERNAL_RE.search(signal_text))
        explicit_provider: str | None = None
        if explicit:
            if self._CLAUDE_SIGNAL_RE.search(signal_text):
                explicit_provider = "claude"
            elif self._GEMINI_SIGNAL_RE.search(signal_text):
                explicit_provider = "gemini"
            elif self._GROQ_SIGNAL_RE.search(signal_text):
                explicit_provider = "groq_inference"
            elif self._OPENROUTER_SIGNAL_RE.search(signal_text):
                explicit_provider = "openrouter"
            else:
                explicit_provider = "grok"  # "use grok" or generic "external"

        # ── Registry-aware selection ───────────────────────────────────────
        # Infer task_type from user_input if not provided by caller.
        if task_type is None:
            task_type = "llm_generate"  # default
            u_lower = signal_text.lower()
            for intent_kw, tt in self._INTENT_TO_TASK_TYPE.items():
                if intent_kw in u_lower:
                    task_type = tt
                    break

        registry = self._provider_registry  # dict loaded once at startup
        _now     = _time.monotonic()

        def _d(extra: dict) -> dict:
            """Attach delegation context to every return dict."""
            extra["delegation_reason"]       = delegation_reason
            extra["expected_output_format"]  = expected_output_format
            return extra

        if registry:
            try:
                # Build eligible provider list:
                #   enabled AND task_type supported AND effective DCL tier allowed
                #   AND not currently rate-limited
                eligible = {
                    name: cfg for name, cfg in registry.items()
                    if cfg.get("enabled")
                    and task_type in cfg.get("task_types", [])
                    and effective_tier in cfg.get("eligible_classifications", [])
                    and not (
                        name in self._provider_rate_limited
                        and _now - self._provider_rate_limited[name] < self._PROVIDER_RATE_LIMIT_TTL_S
                    )
                }

                # Explicit override: honour if eligible, else fall through to default selection
                if explicit_provider and explicit_provider in eligible:
                    chosen = explicit_provider
                    reason = "explicit_override"
                    # alpha_vantage is a data API, not an LLM — tag for audit but keep local
                    if chosen == "alpha_vantage":
                        return _d({
                            "use_external": False, "provider": "alpha_vantage",
                            "adapter": self._PROVIDER_ADAPTERS.get("alpha_vantage", ""),
                            "score": round(score, 3), "penalised_score": round(penalised, 3),
                            "explicit": True, "force_local": False,
                            "reason": "alpha_vantage_data_api",
                        })
                    return _d({
                        "use_external": True, "provider": chosen,
                        "adapter": self._PROVIDER_ADAPTERS.get(chosen, ""),
                        "score": round(score, 3), "penalised_score": round(penalised, 3),
                        "explicit": True, "force_local": False,
                        "reason": reason,
                    })

                # Task-type preferred LLM provider (e.g. grok for news_gather/web_aware_query).
                # Checked BEFORE alpha_vantage so grok wins for "news" queries even if
                # alpha_vantage also supports news_gather.
                preferred = self._TASK_TYPE_PREFERRED.get(task_type)
                if preferred and preferred in eligible:
                    chosen = preferred
                    reason = f"task_type_preference({task_type})"
                    return _d({
                        "use_external": True, "provider": chosen,
                        "adapter": self._PROVIDER_ADAPTERS.get(chosen, ""),
                        "score": round(score, 3), "penalised_score": round(penalised, 3),
                        "explicit": False, "force_local": False,
                        "reason": reason,
                    })

                # Specialist data provider: alpha_vantage matches financial task_types
                # (securities_price, fundamentals, technicals, commodities, economic_indicators).
                # Returns use_external=False — execution engine research harness handles it.
                if "alpha_vantage" in eligible:
                    return _d({
                        "use_external": False, "provider": "alpha_vantage",
                        "adapter": self._PROVIDER_ADAPTERS.get("alpha_vantage", ""),
                        "score": round(score, 3), "penalised_score": round(penalised, 3),
                        "explicit": False, "force_local": False,
                        "reason": "alpha_vantage_data_api",
                    })

                # Complexity-triggered selection among remaining eligible LLM providers
                if penalised >= self._COMPLEXITY_THRESHOLD:
                    # Remove alpha_vantage (data API) from LLM candidates
                    llm_eligible = {k: v for k, v in eligible.items() if k != "alpha_vantage"}
                    if llm_eligible:
                        # Free-first: groq_inference → gemini → openrouter → ollama_cloud → grok (paid last)
                        for pref in self._PROVIDER_QUEUE:
                            if pref in llm_eligible:
                                return _d({
                                    "use_external": True, "provider": pref,
                                    "adapter": self._PROVIDER_ADAPTERS.get(pref, ""),
                                    "score": round(score, 3), "penalised_score": round(penalised, 3),
                                    "explicit": False, "force_local": False,
                                    "reason": "complexity",
                                })

                # No eligible external provider — go local
                return _d({
                    "use_external": False, "provider": "local",
                    "adapter": "local",
                    "score": round(score, 3), "penalised_score": round(penalised, 3),
                    "explicit": False, "force_local": False,
                    "reason": "local_default",
                })

            except Exception as e:
                _log.warning("_routing_decision: registry lookup failed (%s) — complexity fallback", e)
                # Fall through to complexity scorer below

        # ── Complexity scorer fallback (registry empty or lookup failed) ───
        if explicit:
            provider = explicit_provider or "grok"
        elif self._CLAUDE_SIGNAL_RE.search(signal_text):
            provider = "claude"
        elif self._GROK_SIGNAL_RE.search(signal_text):
            provider = "grok"
        else:
            provider = "grok"

        use_external = (explicit or penalised >= self._COMPLEXITY_THRESHOLD) and not force_local
        reason = (
            "explicit_external" if explicit else
            "complexity"        if penalised >= self._COMPLEXITY_THRESHOLD else
            "local_default"
        )
        return _d({
            "use_external":    use_external,
            "provider":        provider if use_external else "local",
            "adapter":         self._PROVIDER_ADAPTERS.get(provider if use_external else "local", "local"),
            "score":           round(score, 3),
            "penalised_score": round(penalised, 3),
            "explicit":        explicit,
            "force_local":     False,
            "reason":          reason,
        })

    async def route_cognition(
        self,
        prompt:   str,
        agent:    str = "sovereign-core",
        system:   str = "You are a helpful assistant.",
        provider: str | None = None,   # None = auto-select via _routing_decision
        user_input: str = "",
    ) -> dict:
        """Local-first cognition routing. See class-level routing comment block.

        Returns {response, provider_used, complexity_score, routed_external}.
        """
        decision = self._routing_decision(prompt, user_input=user_input)
        # Explicit provider arg overrides keyword-based selection.
        # An explicit provider always forces external routing regardless of complexity score.
        chosen = provider or decision["provider"]
        use_external = decision["use_external"] or (provider is not None)

        if use_external:
            _ask_fn = {
                "claude":         self.ask_claude,
                "grok":           self.ask_grok,
                "gemini":         self.ask_gemini,
                "groq_inference": self.ask_groq_inf,
                "ollama_cloud":   self.ask_ollama_cloud,
                "openrouter":     self.ask_openrouter,
            }.get(chosen, self.ask_grok)
            result = await _ask_fn(prompt, agent=agent, system=system)
            result["provider_used"]      = chosen
            result["complexity_score"]   = decision["score"]
            result["penalised_score"]    = decision["penalised_score"]
            result["routed_external"]    = True
            result["explicit_request"]   = decision["explicit"]
            result["routing_reason"]     = decision["reason"]
            return result
        else:
            raw = await self._llm_generate(prompt, model=MODEL)
            return {
                "response":          raw.get("response", ""),
                "provider_used":     "ollama",
                "complexity_score":  decision["score"],
                "penalised_score":   decision["penalised_score"],
                "routed_external":   False,
                "explicit_request":  decision["explicit"],
                "routing_reason":    decision["reason"],
            }

    # ── Task intent parsing ───────────────────────────────────────────────
    async def parse_task_intent(self, user_input: str) -> dict:
        """Parse a natural-language scheduling request into a structured TaskDefinition.

        Returns a dict with keys: needs_clarification, title, schedule, steps,
        notify_when, stop_condition. On ambiguity: needs_clarification=True + question.
        """
        prompt = prompts.task_intent_parser(user_input)
        return await self.call_llm_json(prompt)

    # ── Lesson persistence ────────────────────────────────────────────────
    async def save_lesson(self, fact: str, user_input: str,
                          collection: str = "working_memory",
                          memory_type: str = "lesson",
                          writer: str = "sovereign-core",
                          human_confirmed: bool = False,
                          extra_metadata: dict = None) -> str:
        metadata = {"input": user_input, "type": memory_type}
        if extra_metadata:
            metadata.update(extra_metadata)
        return await self.qdrant.store(
            content=fact,
            metadata=metadata,
            collection=collection,
            writer=writer,
            human_confirmed=human_confirmed,
        )
