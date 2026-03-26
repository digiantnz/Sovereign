import json
import logging
import os
import re as _re_fab
from adapters.ollama import OllamaAdapter
from adapters.grok import GrokAdapter
from adapters.claude import ClaudeAdapter
from cognition import prompts
from cognition.dcl import DisclosureControlLayer

MODEL = "llama3.1:8b-instruct-q4_K_M"
PERSONAS_DIR = "/home/sovereign/personas"
SECURITY_AGENT_PATH = os.path.join(PERSONAS_DIR, "SECURITY_AGENT.md")

TRANSLATOR_PATH = os.path.join(PERSONAS_DIR, "translator.md")

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


class CognitionEngine:
    def __init__(self, qdrant=None, ledger=None):
        self.ollama  = OllamaAdapter()
        self.grok    = GrokAdapter()
        self.claude  = ClaudeAdapter()
        self.dcl     = DisclosureControlLayer()
        self.qdrant  = qdrant
        self.ledger  = ledger   # AuditLedger — injected from main lifespan
        self._security_persona: str = self._load_security_persona()

    def _load_security_persona(self) -> str:
        if os.path.exists(SECURITY_AGENT_PATH):
            with open(SECURITY_AGENT_PATH) as f:
                return f.read()
        return "You are a security evaluator. Return JSON only."

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
            for key in matched_keys[:4]:
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

    # ── JSON-enforced LLM call with one retry ────────────────────────────
    async def call_llm_json(self, prompt: str) -> dict:
        for attempt in range(2):
            result = await self.ollama.generate(prompt, model=MODEL, fmt="json")
            raw = result.get("response", "")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                if attempt == 0:
                    continue  # retry once
                raise ValueError(f"LLM returned invalid JSON after retry: {raw[:200]}")

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
    async def ceo_classify(self, user_input: str, context_window=None) -> dict:
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
        )
        result = await self.call_llm_json(prompt)
        result["_memory_confidence"] = confidence
        result["_memory_gaps"] = gaps
        return result

    async def orchestrator_classify(self, user_input: str, context_window=None) -> dict:
        """PASS 1: Orchestrator classification with routing memory lookup.
        New contract: adds specialist + routing_rationale fields.
        Backward compat: also sets delegate_to = specialist.
        """
        result = await self.ceo_classify(user_input, context_window=context_window)
        # Normalise: ensure both specialist and delegate_to are present
        agent = result.get("specialist") or result.get("delegate_to", "")
        result["specialist"] = agent
        result["delegate_to"] = agent
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
                if decision["provider"] == "claude":
                    raw = await self.ask_claude(prompt, agent=agent_name)
                else:
                    raw = await self.ask_grok(prompt, agent=agent_name)

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
                                  context_window=None) -> dict:
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
        )

        # External routing applies to outbound (research may use Claude/Grok)
        decision = self._routing_decision(prompt, user_input=user_input)
        if decision["use_external"]:
            _log.info("specialist_outbound[%s]: routing to %s", agent_name, decision["provider"])
            try:
                if decision["provider"] == "claude":
                    raw = await self.ask_claude(prompt, agent=agent_name)
                else:
                    raw = await self.ask_grok(prompt, agent=agent_name)
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

        result = await self.call_llm_json(prompt)
        result.setdefault("mode", "outbound")
        result["_routed_external"] = False
        result["_provider"] = "ollama"
        return result

    # ── Pass 3 inbound: Specialist interprets execution result ────────────
    async def specialist_inbound(self, agent_name: str, delegation: dict,
                                 outbound: dict, execution_result: dict) -> dict:
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
        )
        result = await self.call_llm_json(prompt)
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
        result = await self.call_llm_json(prompt)
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
                or "unspecified error"
            )
            logger.info("translator_pass: failure result — bypassing LLM")
            return f"That action failed: {error_detail}"

        # ── Hard pre-check B: empty result — deterministic, no LLM ───────────
        if self._is_result_empty(result_for_translator):
            logger.warning("translator_pass: empty result_for_translator — bypassing LLM (integrity guard)")
            return "I don't have that information in memory or current context."

        # ── Has data — call translator LLM ────────────────────────────────────
        try:
            prompt = prompts.translate_from_orchestrator(
                translator_persona=self.load_translator(),
                result_for_translator=result_for_translator,
                tier=tier,
            )
            raw = await self.ollama.generate(prompt, model=MODEL)
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
            return text
        except Exception:
            return result_for_translator.get("outcome", "")

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
        prompt = prompts.security_eval(
            security_persona=self._security_persona,
            scan_categories=scan_result.categories,
            matched_phrases=scan_result.matched_phrases,
            content_preview=content[:500],
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
            raw = await self.ollama.generate(prompt, model=MODEL)
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
        system = (
            "You are Sovereign, a helpful personal AI assistant. Answer directly and helpfully in plain English.\n"
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
            for t in turns[-3:]:
                messages.append({"role": "user", "content": t.get("user", "")})
                messages.append({"role": "assistant", "content": t.get("assistant", "")})
        messages.append({"role": "user", "content": user_input})
        return await self.ollama.chat(messages, model=MODEL)

    # ── Direct query (existing /query route) ─────────────────────────────
    async def ask_local(self, prompt: str, model: str = MODEL) -> dict:
        return await self.ollama.generate(prompt, model=model)

    # ── External cognition — Grok ─────────────────────────────────────────
    async def ask_grok(
        self,
        prompt:  str,
        agent:   str = "sovereign-core",
        system:  str = "You are a helpful assistant.",
        model:   str | None = None,
    ) -> dict:
        """DCL-gated Grok call. Returns {response} or {error} if blocked.
        Every call — including blocks — is logged to audit."""
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

    # ── Routed cognition (local-first, external on complexity/explicit request) ──
    #
    # ROUTING LOGIC (for Rex's self-diagnostic):
    #
    # Step 1 — Explicit override:
    #   "use claude" / "ask claude" / "architectural" / "plan" / "review" /
    #   "design" / "strategy"                            → provider = claude
    #   "use grok" / "ask grok" / "current" / "latest" /
    #   "news" / "today" / "recent" / "market"          → provider = grok
    #   (both trigger external regardless of score)
    #
    # Step 2 — DCL sensitivity gate:
    #   PRIVATE or SECRET content → hard local, no external call ever
    #
    # Step 3 — Complexity scoring (five factors, score in [0,1]):
    #   • Length       (>300 words)                     weight 0.40
    #   • Conjunctions (and/also/furthermore/moreover)   weight 0.20
    #   • Depth kw     (analyse/compare/evaluate/etc.)   weight 0.25
    #   • Question cnt (multiple ?)                      weight 0.15
    #   • Operational penalty: if score ≥ 0.50 AND prompt contains
    #     restart/container/service/deploy/mount/volume/port → -0.20
    #     (biases operational/infra queries back to local Ollama)
    #
    # Step 4 — score ≥ 0.50 (after penalty) → external, default provider = grok
    #
    # Step 5 — Default → Ollama local
    #
    # ALL external calls are DCL-gated and audit-logged regardless of trigger.
    # PASS 1 (classify), PASS 3 (evaluate), PASS 4 (memory) are NEVER routed
    # externally — governance must remain deterministic and local.

    _COMPLEXITY_THRESHOLD = 0.50
    _PREFER_LOCAL_TIERS   = {"PRIVATE", "SECRET"}

    _EXPLICIT_EXTERNAL_RE = __import__("re").compile(
        r"\b(use claude|use grok|ask claude|ask grok|via claude|via grok"
        r"|external llm|external model|external ai)\b",
        __import__("re").IGNORECASE,
    )
    # Provider-selection signals (checked against raw user input, not full prompt)
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
    # Operational/infra keywords that trigger the complexity penalty
    _OPERATIONAL_RE = __import__("re").compile(
        r"\b(restart|container|service|deploy|mount|volume|port|compose|dockerfile"
        r"|nginx|redis|mariadb|healthcheck|network|subnet)\b",
        __import__("re").IGNORECASE,
    )

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

    def _routing_decision(self, prompt: str, user_input: str = "") -> dict:
        """Compute routing decision for a prompt. Returns a dict:
          {use_external, provider, score, penalised_score, explicit, force_local, reason}
        Centralises all routing logic so specialist_reason and route_cognition share it.
        user_input is the raw Director message (for provider-signal matching).
        """
        signal_text = user_input or prompt
        explicit    = bool(self._EXPLICIT_EXTERNAL_RE.search(signal_text))
        # Score complexity on user_input (Director's message) only, not the full
        # specialist prompt — personas are inherently long and would inflate every score.
        score       = self._complexity_score(user_input or prompt)
        tier        = self.dcl.classify(prompt)
        force_local = tier in self._PREFER_LOCAL_TIERS

        # Operational penalty: score ≥ threshold but strongly infra-flavoured → bias local
        penalised = score
        if score >= self._COMPLEXITY_THRESHOLD and self._OPERATIONAL_RE.search(prompt):
            penalised = max(0.0, score - 0.20)

        use_external = (explicit or penalised >= self._COMPLEXITY_THRESHOLD) and not force_local

        # Provider selection from signal keywords; default grok
        if self._CLAUDE_SIGNAL_RE.search(signal_text):
            provider = "claude"
        elif self._GROK_SIGNAL_RE.search(signal_text):
            provider = "grok"
        else:
            provider = "grok"  # default for complexity-triggered external calls

        reason = (
            "force_local(dcl)"   if force_local else
            "explicit_external"  if explicit else
            "complexity"         if penalised >= self._COMPLEXITY_THRESHOLD else
            "local_default"
        )
        return {
            "use_external":   use_external,
            "provider":       provider,
            "score":          round(score, 3),
            "penalised_score": round(penalised, 3),
            "explicit":       explicit,
            "force_local":    force_local,
            "reason":         reason,
        }

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
        # Explicit provider arg overrides keyword-based selection
        chosen = provider or decision["provider"]

        if decision["use_external"]:
            if chosen == "claude":
                result = await self.ask_claude(prompt, agent=agent, system=system)
            else:
                result = await self.ask_grok(prompt, agent=agent, system=system)
            result["provider_used"]      = chosen
            result["complexity_score"]   = decision["score"]
            result["penalised_score"]    = decision["penalised_score"]
            result["routed_external"]    = True
            result["explicit_request"]   = decision["explicit"]
            result["routing_reason"]     = decision["reason"]
            return result
        else:
            raw = await self.ollama.generate(prompt, model=MODEL)
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
