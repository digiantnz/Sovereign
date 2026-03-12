import json
import os
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

    async def load_memory_context(self, query: str,
                                   query_type: str = "knowledge") -> tuple[str, float, list[str]]:
        """Search all 7 sovereign collections with context-aware weighting.
        query_type: action | knowledge | session_start — controls collection score weights.
        On very low confidence (< 0.5), ensures a gap entry exists in meta.
        """
        try:
            results = await self.qdrant.search_all_weighted(
                query, query_type=query_type, top_k=3
            )
            confidence = self.qdrant.compute_confidence(results)
            gaps = self.qdrant.get_gaps(results)

            # Priority 5: very low confidence → ensure meta gap entry exists
            if confidence < 0.5:
                await self.qdrant.ensure_gap_entry(query)

            if not results:
                return "", confidence, gaps
            lines = [
                f"- [{r.get('_collection', '?')}|{r.get('timestamp', '')[:10]}|"
                f"{r['score']:.2f}(w={r.get('_weight', 1.0):.1f})] {r.get('content', '')}"
                for r in results
            ]
            return "Relevant memories:\n" + "\n".join(lines), confidence, gaps
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

    # ── Pass 2: Specialist Reasoning ──────────────────────────────────────
    async def specialist_reason(self, agent_name: str, delegation: dict, user_input: str) -> dict:
        from skills.loader import SkillLoader
        persona = self.load_persona(agent_name)
        try:
            loader = SkillLoader(agent_name, ledger=self.ledger)
            persona = loader.inject_into_persona(persona)
            if loader.skills:
                import logging as _log
                _log.getLogger(__name__).debug(
                    "SkillLoader: injected %s into %s persona",
                    loader.get_skill_names(), agent_name,
                )
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).warning("SkillLoader: failed for %s: %s", agent_name, e)
        prompt = prompts.specialist(
            agent_persona=persona,
            delegation=delegation,
            user_input=user_input,
        )
        return await self.call_llm_json(prompt)

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
            text = _re2.sub(
                r"\n*(This message meets|Here is the translated|Communication preference|"
                r"Director communication|Please note that|Note:|---+).*",
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
    # Complexity threshold: prompt scores above this → eligible for external routing
    _COMPLEXITY_THRESHOLD = 0.50

    # Explicit external-routing keywords from Director
    _EXPLICIT_EXTERNAL_RE = __import__("re").compile(
        r"\b(use claude|use grok|ask claude|ask grok|via claude|via grok"
        r"|external llm|external model|external ai)\b",
        __import__("re").IGNORECASE,
    )

    _PREFER_LOCAL_TIERS = {"PRIVATE", "SECRET"}  # hard-prefer local regardless of complexity

    @staticmethod
    def _complexity_score(prompt: str) -> float:
        """Heuristic complexity score in [0, 1]. Higher = more complex.

        Factors:
          - Length (>300 words → high complexity)
          - Presence of multi-part conjunctions (and/also/additionally/furthermore)
          - Technical depth markers (analyse, synthesise, compare, evaluate, contrast)
          - Question count (multiple questions in one prompt)
        """
        words      = prompt.split()
        length_s   = min(len(words) / 300, 1.0)                               # 0–1

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

    async def route_cognition(
        self,
        prompt:    str,
        agent:     str = "sovereign-core",
        system:    str = "You are a helpful assistant.",
        provider:  str = "grok",               # preferred external provider if routed externally
    ) -> dict:
        """Local-first cognition routing per EXTERNAL_COGNITION.md.

        Decision flow:
          1. Explicit Director request → external (DCL still applies)
          2. Sensitivity tier PRIVATE/SECRET → local only
          3. Complexity score >= threshold → external (DCL still applies)
          4. Default → local Ollama

        Returns {response, provider_used, complexity_score, routed_external}.
        """
        explicit  = bool(self._EXPLICIT_EXTERNAL_RE.search(prompt))
        score     = self._complexity_score(prompt)
        tier      = self.dcl.classify(prompt)
        force_local = tier in self._PREFER_LOCAL_TIERS

        use_external = (explicit or score >= self._COMPLEXITY_THRESHOLD) and not force_local

        if use_external:
            if provider == "claude":
                result = await self.ask_claude(prompt, agent=agent, system=system)
            else:
                result = await self.ask_grok(prompt, agent=agent, system=system)
            result["provider_used"]     = provider
            result["complexity_score"]  = round(score, 3)
            result["routed_external"]   = True
            result["explicit_request"]  = explicit
            return result
        else:
            raw = await self.ollama.generate(prompt, model=MODEL)
            return {
                "response":          raw.get("response", ""),
                "provider_used":     "ollama",
                "complexity_score":  round(score, 3),
                "routed_external":   False,
                "explicit_request":  explicit,
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
