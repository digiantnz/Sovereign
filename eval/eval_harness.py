#!/usr/bin/env python3
"""
eval_harness.py — Shadow evaluation harness for Sovereign AI model selection.

Compares two Ollama models against the sovereign eval corpus without touching
sovereign-core, governance.json, or the live OLLAMA_MODEL config.

Usage:
    python3 eval_harness.py \\
        --model-a hf.co/RoadToNowhere/Qwen3-32B-abliterated-Q4_K_M-GGUF:latest \\
        --model-b huihui_ai/glm-4.7-flash-abliterated:q4_K

Outputs:
    /docker/sovereign/eval/report.md
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── Paths ──────────────────────────────────────────────────────────────────────

CORPUS_PATH  = Path("/docker/sovereign/eval/corpus.json")
REPORT_PATH  = Path("/docker/sovereign/eval/report.md")
PERSONAS_DIR = Path("/home/sovereign/personas")
SOVEREIGN_CORE = Path("/home/sovereign/sovereign/core/app")
OLLAMA_URL   = "http://localhost:11434"
NUM_CTX      = 20480  # matches production _NUM_CTX in adapters/ollama.py; 32k OOM-risks RTX 3090
POLL_INTERVAL = 2     # seconds between /api/ps polls when unloading
UNLOAD_TIMEOUT = 30   # seconds before hard-fail on unload

# ── Sovereign prompt imports ───────────────────────────────────────────────────

sys.path.insert(0, str(SOVEREIGN_CORE))
from cognition import prompts as _prompts  # noqa: E402  (post-path-insert)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_persona(name: str) -> str:
    """Load persona markdown from /home/sovereign/personas/<name>.md."""
    for candidate in [name, name.upper()]:
        p = PERSONAS_DIR / f"{candidate}.md"
        if p.exists():
            return p.read_text()
    return f"# {name}\nYou are {name}, a specialist agent for Sovereign AI."


def _split_prompt(full_prompt: str) -> tuple[str, str]:
    """
    Split the full prompt string returned by sovereign prompts functions into
    (system_content, user_content) so /api/chat can apply the correct chat
    template per model.

    Sovereign prompts start with the persona block, then a '---' separator
    before the task section.  The first '---' after the persona is the split
    point.  When no separator is found the entire string is sent as user content.
    """
    parts = full_prompt.split("\n---\n", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", full_prompt.strip()


def _build_messages(system: str, user: str, model_name: str) -> list[dict]:
    """
    Build the /api/chat messages list.  Prepend /no_think for Qwen3 models to
    suppress thinking tokens; GLM does not understand this directive so skip it.
    """
    no_think_prefix = ""
    if "qwen3" in model_name.lower() or "qwen-3" in model_name.lower():
        no_think_prefix = "/no_think\n"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": no_think_prefix + user})
    return messages


def _build_pass1_prompt(case: dict) -> tuple[str, str]:
    persona = _load_persona("orchestrator")
    full = _prompts.classify(
        ceo_persona=persona,
        user_input=case["input_text"],
        memory_context="No prior context loaded for evaluation run.",
    )
    return _split_prompt(full)


def _build_pass3a_prompt(case: dict) -> tuple[str, str]:
    delegation = case.get("delegation", {})
    agent = delegation.get("delegate_to", "research_agent")
    persona = _load_persona(agent)
    full = _prompts.specialist_outbound(
        agent_persona=persona,
        delegation=delegation,
        user_input=case["input_text"],
    )
    return _split_prompt(full)


def _build_pass4_prompt(case: dict) -> tuple[str, str]:
    persona = _load_persona("orchestrator")
    delegation = {
        "intent": case.get("intent", "query"),
        "tier":   case.get("tier", "LOW"),
    }
    specialist_result = case.get("specialist_inbound_result", {
        "success": True, "outcome": "Completed.", "detail": {}, "error": None,
    })
    full = _prompts.orchestrator_evaluate(
        orchestrator_persona=persona,
        delegation=delegation,
        specialist_inbound_result=specialist_result,
    )
    return _split_prompt(full)


# ── JSON parsing (mirrors _parse_llm_output in engine.py) ─────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*")


def _parse_response(raw: str) -> tuple[dict | None, bool]:
    """
    Parse raw LLM text into a dict.
    Returns (parsed_dict, was_repaired).
    was_repaired=True means json.loads failed on first attempt (counts toward json_repair_rate).
    """
    cleaned = _FENCE_RE.sub("", raw).replace("```", "").strip()

    # First attempt — clean direct parse
    try:
        return json.loads(cleaned), False
    except json.JSONDecodeError:
        pass

    # Repair attempt — regex extract first {...} block
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group()), True
        except json.JSONDecodeError:
            pass

    return None, True  # total parse failure; also counts as repaired


# ── Schema validation ──────────────────────────────────────────────────────────

def _check_schema(parsed: dict | None, expected_schema: dict) -> tuple[bool, list[str]]:
    """
    Validate parsed JSON against expected_json_schema from corpus.
    Returns (all_passed, list_of_failures).
    Handles nested result_for_translator.fields sub-schema.
    """
    if parsed is None:
        return False, ["total parse failure — no JSON extracted"]

    failures = []

    for key, spec in expected_schema.items():
        if not isinstance(spec, dict):
            continue

        if key == "result_for_translator" and "fields" in spec:
            rft = parsed.get("result_for_translator")
            if rft is None:
                failures.append("result_for_translator: missing")
                continue
            for sub_key, sub_spec in spec["fields"].items():
                if not isinstance(sub_spec, dict):
                    continue
                if sub_spec.get("required") and sub_key not in rft:
                    failures.append(f"result_for_translator.{sub_key}: required but missing")
                    continue
                if sub_key in rft:
                    val = rft[sub_key]
                    if "accepted_values" in sub_spec:
                        if val not in sub_spec["accepted_values"]:
                            failures.append(
                                f"result_for_translator.{sub_key}: got {val!r}, "
                                f"expected one of {sub_spec['accepted_values']}"
                            )
                    elif sub_spec.get("tolerance") == "non_empty":
                        if not val:
                            failures.append(
                                f"result_for_translator.{sub_key}: must be non-empty"
                            )
            continue

        val = parsed.get(key)

        if spec.get("required") and key not in parsed:
            nullable = spec.get("nullable", False)
            if not nullable:
                failures.append(f"{key}: required but missing")
            continue

        if key not in parsed:
            continue

        if "accepted_values" in spec:
            if val not in spec["accepted_values"]:
                failures.append(
                    f"{key}: got {val!r}, expected one of {spec['accepted_values']}"
                )
        elif spec.get("tolerance") == "non_empty":
            if not val:
                failures.append(f"{key}: must be non-empty, got {val!r}")
        elif spec.get("tolerance") == "contains" and "value" in spec:
            if spec["value"].lower() not in str(val).lower():
                failures.append(
                    f"{key}: expected to contain {spec['value']!r}, got {val!r}"
                )
        elif "pattern" in spec:
            if not re.match(spec["pattern"], str(val) if val else ""):
                failures.append(
                    f"{key}: {val!r} does not match pattern {spec['pattern']!r}"
                )

    return len(failures) == 0, failures


# ── VRAM sampling ──────────────────────────────────────────────────────────────

def _sample_vram_mb() -> int | None:
    """Sample peak VRAM via nvidia-smi. Returns MiB or None on failure."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            timeout=5, text=True,
        )
        return int(out.strip().split("\n")[0].strip())
    except Exception:
        return None


# ── Ollama API calls ───────────────────────────────────────────────────────────

def _chat_stream(model: str, messages: list[dict]) -> dict:
    """
    Call /api/chat with stream=True and capture:
      - ttft_ms: time to first non-empty content token (milliseconds)
      - total_ms: wall-clock from request to final token
      - raw_content: full assembled response text
      - vram_mid_mb: VRAM sampled partway through generation

    Returns dict with all keys above.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {
            "num_ctx": NUM_CTX,
        },
    }

    t_start = time.monotonic()
    ttft_ms: float | None = None
    vram_mid_mb: int | None = None
    chunks: list[str] = []
    chunk_count = 0
    vram_sample_trigger = 5  # sample VRAM after 5th content chunk

    try:
        with requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            stream=True,
            timeout=120,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                content = evt.get("message", {}).get("content", "")
                if content:
                    chunk_count += 1
                    chunks.append(content)

                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - t_start) * 1000

                    if chunk_count == vram_sample_trigger:
                        vram_mid_mb = _sample_vram_mb()

                if evt.get("done"):
                    break

    except requests.exceptions.Timeout:
        return {
            "raw_content": "".join(chunks),
            "ttft_ms": ttft_ms,
            "total_ms": (time.monotonic() - t_start) * 1000,
            "vram_mid_mb": vram_mid_mb,
            "error": "timeout",
        }
    except Exception as exc:
        return {
            "raw_content": "".join(chunks),
            "ttft_ms": ttft_ms,
            "total_ms": (time.monotonic() - t_start) * 1000,
            "vram_mid_mb": vram_mid_mb,
            "error": str(exc),
        }

    return {
        "raw_content": "".join(chunks),
        "ttft_ms": ttft_ms,
        "total_ms": (time.monotonic() - t_start) * 1000,
        "vram_mid_mb": vram_mid_mb,
        "error": None,
    }


def _loaded_models() -> list[str]:
    """Return list of currently-loaded model names from /api/ps."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _unload_model(model: str) -> bool:
    """
    Request unload via keep_alive=0 then poll /api/ps until the model
    disappears or timeout.  Returns True on clean unload, False on timeout.
    """
    print(f"  [unload] requesting unload of {model}")
    try:
        requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": model, "messages": [], "keep_alive": 0},
            timeout=15,
        )
    except Exception:
        pass  # request may fail if nothing loaded; still poll

    deadline = time.monotonic() + UNLOAD_TIMEOUT
    while time.monotonic() < deadline:
        loaded = _loaded_models()
        if model not in loaded:
            print(f"  [unload] {model} unloaded cleanly")
            return True
        print(f"  [unload] waiting… still loaded: {loaded}")
        time.sleep(POLL_INTERVAL)

    print(f"  [unload] TIMEOUT — {model} still loaded after {UNLOAD_TIMEOUT}s")
    return False


def _warmup(model: str) -> None:
    """
    Send a minimal prompt to pre-load the model into VRAM before timed evaluation.
    This avoids counting model load time in the first test case's latency.
    """
    print(f"  [warmup] loading {model} into VRAM…")
    _chat_stream(model, [{"role": "user", "content": "Say ok"}])
    print(f"  [warmup] done")


# ── Per-case evaluation ────────────────────────────────────────────────────────

def _run_case(model: str, case: dict, case_num: int, total: int) -> dict:
    """
    Build the correct prompt for the case's pass type, run it, parse output,
    and return a full result record.
    """
    pass_type = str(case.get("pass", "1"))
    case_id = case.get("id", f"case-{case_num}")

    print(f"  [{case_num}/{total}] {case_id} (PASS {pass_type})")

    # Build prompt
    try:
        if pass_type == "1":
            sys_content, usr_content = _build_pass1_prompt(case)
        elif pass_type in ("3", "3a"):
            sys_content, usr_content = _build_pass3a_prompt(case)
        elif pass_type == "4":
            sys_content, usr_content = _build_pass4_prompt(case)
        else:
            return {"case_id": case_id, "error": f"unknown pass type: {pass_type}"}
    except Exception as exc:
        return {"case_id": case_id, "error": f"prompt build failed: {exc}"}

    messages = _build_messages(sys_content, usr_content, model)

    # Run inference
    resp = _chat_stream(model, messages)
    if resp.get("error") and not resp.get("raw_content"):
        return {
            "case_id": case_id,
            "pass": pass_type,
            "error": resp["error"],
            "ttft_ms": resp.get("ttft_ms"),
            "total_ms": resp.get("total_ms"),
            "vram_mid_mb": resp.get("vram_mid_mb"),
            "schema_pass": False,
            "schema_failures": ["inference error: " + resp["error"]],
            "json_repaired": True,
            "parsed": None,
            "raw": resp.get("raw_content", ""),
        }

    # Parse JSON
    parsed, was_repaired = _parse_response(resp["raw_content"])

    # Schema check
    schema_pass, schema_failures = _check_schema(parsed, case.get("expected_json_schema", {}))

    result = {
        "case_id": case_id,
        "pass": pass_type,
        "description": case.get("description", ""),
        "source": case.get("source", ""),
        "ttft_ms": round(resp["ttft_ms"], 1) if resp["ttft_ms"] else None,
        "total_ms": round(resp["total_ms"], 1),
        "vram_mid_mb": resp["vram_mid_mb"],
        "json_repaired": was_repaired,
        "parsed": parsed,
        "schema_pass": schema_pass,
        "schema_failures": schema_failures,
        "error": resp.get("error"),
    }

    # PASS 1-specific: intent/tier match
    if pass_type == "1" and parsed:
        result["intent_match"] = (
            parsed.get("intent") in case.get("expected_json_schema", {})
            .get("intent", {}).get("accepted_values", [parsed.get("intent")])
        )
        result["tier_match"] = (
            parsed.get("tier") == case.get("expected_tier")
        )
        result["got_intent"] = parsed.get("intent")
        result["got_tier"] = parsed.get("tier")

    status = "PASS" if schema_pass else "FAIL"
    repair = " [repaired]" if was_repaired else ""
    print(f"          {status}{repair} — {resp['total_ms']:.0f}ms total"
          + (f", TTFT {resp['ttft_ms']:.0f}ms" if resp["ttft_ms"] else ""))
    if schema_failures:
        for f in schema_failures[:2]:
            print(f"          ✗ {f}")

    return result


# ── Model run ──────────────────────────────────────────────────────────────────

def run_model(model: str, cases: list[dict]) -> list[dict]:
    """Run all corpus cases against one model. Returns list of result records."""
    print(f"\n{'='*60}")
    print(f"MODEL: {model}")
    print(f"{'='*60}")

    _warmup(model)

    results = []
    for i, case in enumerate(cases, 1):
        result = _run_case(model, case, i, len(cases))
        result["model"] = model
        results.append(result)

    return results


# ── Report generation ──────────────────────────────────────────────────────────

def _pct(n: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{100*n//total}% ({n}/{total})"


def _mean(vals: list[float]) -> str:
    clean = [v for v in vals if v is not None]
    if not clean:
        return "—"
    return f"{sum(clean)/len(clean):.0f}"


def _pass_results(results: list[dict], pass_type: str) -> list[dict]:
    return [r for r in results if str(r.get("pass")) == str(pass_type)]


def generate_report(
    model_a: str,
    model_b: str,
    results_a: list[dict],
    results_b: list[dict],
    vram_info: dict,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Sovereign Model Evaluation Report",
        f"",
        f"Generated: {now}  ",
        f"Corpus: `{CORPUS_PATH}`  ",
        f"Model A: `{model_a}`  ",
        f"Model B: `{model_b}`  ",
        f"num_ctx: {NUM_CTX} (matches production `_NUM_CTX` in adapters/ollama.py; 32768 OOM-risks RTX 3090)  ",
        f"Thinking tokens: disabled (Qwen3: `/no_think` prefix; GLM: no directive)",
        f"",
    ]

    # VRAM section
    lines += [
        "## VRAM Footprint",
        "",
        f"| Model | On-disk size | Peak VRAM (mid-gen, num_ctx={NUM_CTX}) |",
        "|-------|-------------|----------------------------------------|",
    ]
    for label, model, res in [("A", model_a, results_a), ("B", model_b, results_b)]:
        vram_samples = [r["vram_mid_mb"] for r in res if r.get("vram_mid_mb")]
        peak = max(vram_samples) if vram_samples else None
        disk = vram_info.get(model, {}).get("disk_gb", "—")
        peak_str = f"{peak} MiB ({peak/1024:.1f} GB)" if peak else "—"
        lines.append(f"| {label}: `{model}` | {disk} GB | {peak_str} |")
    lines.append("")
    lines.append(
        "> **Note:** RTX 3090 total VRAM = 24 GB. OOM risk if loaded model + num_ctx KV cache > 24 GB."
    )
    lines.append("")

    # Per-pass summary tables
    for pass_label, pass_key in [("PASS 1 — Classification", "1"), ("PASS 3a — Skill Payload", "3a"), ("PASS 4 — Evaluation", "4")]:
        ra = _pass_results(results_a, pass_key)
        rb = _pass_results(results_b, pass_key)

        if not ra and not rb:
            continue

        lines += [f"## {pass_label}", ""]

        # Summary row
        lines += [
            "| Metric | Model A | Model B |",
            "|--------|---------|---------|",
        ]

        # Schema pass rate
        a_pass = sum(1 for r in ra if r.get("schema_pass"))
        b_pass = sum(1 for r in rb if r.get("schema_pass"))
        lines.append(f"| Schema pass rate | {_pct(a_pass, len(ra))} | {_pct(b_pass, len(rb))} |")

        # JSON repair rate
        a_repair = sum(1 for r in ra if r.get("json_repaired"))
        b_repair = sum(1 for r in rb if r.get("json_repaired"))
        lines.append(f"| JSON repair rate | {_pct(a_repair, len(ra))} | {_pct(b_repair, len(rb))} |")

        # PASS 1: intent/tier match
        if pass_key == "1":
            a_intent = sum(1 for r in ra if r.get("intent_match"))
            b_intent = sum(1 for r in rb if r.get("intent_match"))
            a_tier = sum(1 for r in ra if r.get("tier_match"))
            b_tier = sum(1 for r in rb if r.get("tier_match"))
            lines.append(f"| Intent match | {_pct(a_intent, len(ra))} | {_pct(b_intent, len(rb))} |")
            lines.append(f"| Tier match | {_pct(a_tier, len(ra))} | {_pct(b_tier, len(rb))} |")

        # Latency
        a_ttft = _mean([r.get("ttft_ms") for r in ra])
        b_ttft = _mean([r.get("ttft_ms") for r in rb])
        a_total = _mean([r.get("total_ms") for r in ra])
        b_total = _mean([r.get("total_ms") for r in rb])
        lines.append(f"| Mean TTFT | {a_ttft} ms | {b_ttft} ms |")
        lines.append(f"| Mean total latency | {a_total} ms | {b_total} ms |")
        lines.append("")

        # Per-case detail
        lines.append(f"### Per-case results ({pass_label})")
        lines.append("")
        lines.append("| Case | A result | A ms | B result | B ms | Notes |")
        lines.append("|------|----------|------|----------|------|-------|")

        all_ids = {r["case_id"] for r in ra} | {r["case_id"] for r in rb}
        a_by_id = {r["case_id"]: r for r in ra}
        b_by_id = {r["case_id"]: r for r in rb}

        for case_id in sorted(all_ids):
            ra_c = a_by_id.get(case_id, {})
            rb_c = b_by_id.get(case_id, {})

            def fmt_result(r: dict) -> str:
                if not r:
                    return "—"
                if r.get("schema_pass"):
                    repair = " ⚠repair" if r.get("json_repaired") else ""
                    return f"✓{repair}"
                fails = r.get("schema_failures", [])
                short = fails[0][:40] if fails else "?"
                return f"✗ {short}"

            def fmt_ms(r: dict) -> str:
                t = r.get("total_ms")
                return f"{t:.0f}" if t else "—"

            # Extra notes for PASS 1
            note = ""
            if pass_key == "1":
                a_intent = ra_c.get("got_intent", "—")
                b_intent = rb_c.get("got_intent", "—")
                if a_intent != b_intent:
                    note = f"A→{a_intent} B→{b_intent}"

            desc = ra_c.get("description") or rb_c.get("description", "")
            short_desc = desc[:35] + "…" if len(desc) > 35 else desc
            lines.append(
                f"| {case_id}<br/>{short_desc} | {fmt_result(ra_c)} | {fmt_ms(ra_c)} | "
                f"{fmt_result(rb_c)} | {fmt_ms(rb_c)} | {note} |"
            )

        lines.append("")

    # Failure detail
    lines += ["## Failure Detail", ""]
    any_failures = False
    for label, results in [("Model A", results_a), ("Model B", results_b)]:
        for r in results:
            if not r.get("schema_pass"):
                any_failures = True
                lines.append(f"### {label} — {r['case_id']} (PASS {r.get('pass')})")
                lines.append(f"**{r.get('description', '')}**  ")
                lines.append(f"Source: {r.get('source', '')}  ")
                if r.get("error"):
                    lines.append(f"Error: `{r['error']}`  ")
                for f in r.get("schema_failures", []):
                    lines.append(f"- ✗ {f}")
                if r.get("parsed"):
                    pretty = json.dumps(r["parsed"], indent=2)[:800]
                    lines.append(f"\nParsed output (truncated):\n```json\n{pretty}\n```")
                elif r.get("raw"):
                    raw_trunc = r["raw"][:400]
                    lines.append(f"\nRaw output (truncated):\n```\n{raw_trunc}\n```")
                lines.append("")

    if not any_failures:
        lines.append("No failures. All cases passed schema validation for both models.")
        lines.append("")

    # Recommendation
    lines += ["## Recommendation", ""]

    total_a = len(results_a)
    total_b = len(results_b)
    pass_a = sum(1 for r in results_a if r.get("schema_pass"))
    pass_b = sum(1 for r in results_b if r.get("schema_pass"))
    repair_a = sum(1 for r in results_a if r.get("json_repaired"))
    repair_b = sum(1 for r in results_b if r.get("json_repaired"))
    ttft_a_vals = [r["ttft_ms"] for r in results_a if r.get("ttft_ms")]
    ttft_b_vals = [r["ttft_ms"] for r in results_b if r.get("ttft_ms")]
    mean_ttft_a = sum(ttft_a_vals) / len(ttft_a_vals) if ttft_a_vals else None
    mean_ttft_b = sum(ttft_b_vals) / len(ttft_b_vals) if ttft_b_vals else None

    a_pct = 100 * pass_a // total_a if total_a else 0
    b_pct = 100 * pass_b // total_b if total_b else 0

    lines.append(f"| | Model A | Model B |")
    lines.append(f"|--|---------|---------|")
    lines.append(f"| Model | `{model_a}` | `{model_b}` |")
    lines.append(f"| Overall schema pass | {_pct(pass_a, total_a)} | {_pct(pass_b, total_b)} |")
    lines.append(f"| JSON repair rate | {_pct(repair_a, total_a)} | {_pct(repair_b, total_b)} |")
    lines.append(f"| Mean TTFT | {f'{mean_ttft_a:.0f} ms' if mean_ttft_a else '—'} | {f'{mean_ttft_b:.0f} ms' if mean_ttft_b else '—'} |")
    lines.append("")

    delta = a_pct - b_pct
    if abs(delta) < 5:
        verdict = (
            f"**Both models perform similarly on this corpus** ({a_pct}% vs {b_pct}% schema pass). "
            f"Latency and JSON reliability are the differentiating factors. "
            f"Review per-case failures above before cutover decision."
        )
    elif delta > 0:
        verdict = (
            f"**Model A ({model_a}) outperforms Model B** on schema validity ({a_pct}% vs {b_pct}%). "
            f"Recommend retaining Model A unless latency or VRAM constraints favour B. "
            f"Director review required before any production cutover."
        )
    else:
        verdict = (
            f"**Model B ({model_b}) outperforms Model A** on schema validity ({b_pct}% vs {a_pct}%). "
            f"Consider switching to Model B — validate against extended corpus and live routing logs "
            f"before production cutover. Director review required."
        )

    lines.append(verdict)
    lines.append("")
    lines.append(
        "> **This report is advisory only.** No production changes should be made without "
        "Director review. Validate against live Telegram routing logs before any model swap."
    )
    lines.append("")

    return "\n".join(lines)


# ── Disk size query ────────────────────────────────────────────────────────────

def _get_disk_sizes(models: list[str]) -> dict:
    """Query ollama list (inside container) for model sizes. Returns {name: {disk_gb: float}}."""
    result = {}
    try:
        out = subprocess.check_output(
            ["docker", "exec", "ollama", "ollama", "list"],
            timeout=15, text=True,
        )
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                name = parts[0]
                size_str = parts[2]
                try:
                    size_gb = float(size_str)
                    result[name] = {"disk_gb": size_gb}
                except ValueError:
                    pass
    except Exception:
        pass
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shadow model evaluation harness for Sovereign AI"
    )
    parser.add_argument(
        "--model-a",
        default="hf.co/RoadToNowhere/Qwen3-32B-abliterated-Q4_K_M-GGUF:latest",
        help="Model A (default: current production Qwen3-32B)",
    )
    parser.add_argument(
        "--model-b",
        default="huihui_ai/glm-4.7-flash-abliterated:q4_K",
        help="Model B (default: GLM-4.7-flash candidate)",
    )
    parser.add_argument(
        "--corpus",
        default=str(CORPUS_PATH),
        help="Path to corpus JSON file",
    )
    parser.add_argument(
        "--output",
        default=str(REPORT_PATH),
        help="Path for markdown report output",
    )
    parser.add_argument(
        "--passes",
        default="1,3a,4",
        help="Comma-separated pass types to run (e.g. '1,4' to skip 3a)",
    )
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text())
    all_cases = corpus["cases"]

    # Filter by requested passes
    allowed_passes = {p.strip() for p in args.passes.split(",")}
    cases = [c for c in all_cases if str(c.get("pass")) in allowed_passes]
    print(f"Loaded {len(cases)} cases from corpus (passes: {', '.join(sorted(allowed_passes))})")

    vram_info = _get_disk_sizes([args.model_a, args.model_b])

    # ── Run Model A ──────────────────────────────────────────────────────────
    results_a = run_model(args.model_a, cases)

    # Unload Model A before loading Model B
    unloaded = _unload_model(args.model_a)
    if not unloaded:
        print("WARNING: Model A unload timed out. Model B may OOM. Continuing anyway.")

    # ── Run Model B ──────────────────────────────────────────────────────────
    results_b = run_model(args.model_b, cases)

    # Unload Model B (clean up)
    _unload_model(args.model_b)

    # ── Generate report ──────────────────────────────────────────────────────
    report = generate_report(args.model_a, args.model_b, results_a, results_b, vram_info)
    out_path = Path(args.output)
    out_path.write_text(report)
    print(f"\nReport written to: {out_path}")

    # Summary to stdout
    total = len(cases)
    pass_a = sum(1 for r in results_a if r.get("schema_pass"))
    pass_b = sum(1 for r in results_b if r.get("schema_pass"))
    print(f"\nFinal: Model A {pass_a}/{total} passed | Model B {pass_b}/{total} passed")


if __name__ == "__main__":
    main()
