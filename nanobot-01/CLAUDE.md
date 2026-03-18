# nanobot-01 — Implementation Invariants

This file is loaded by Claude Code when working inside `nanobot-01/`. It supplements the root `CLAUDE.md`.

---

## Role and Boundary

- nanobot-01 is the primary skill execution environment for all application-level skills
- **Hard boundary**: docker-broker handles ONLY system calls (SYSTEM_COMMANDS whitelist); nanobot-01 handles all application skills (IMAP/SMTP/WebDAV/CalDAV/feeds/python3 scripts)
- Container: ai_net + business_net (business_net required for nextcloud.py → `http://nextcloud`)
- Port: 8080 on ai_net

---

## Protocol Contract — A2A JSON-RPC 3.0

Wire format via `sovereign_a2a` package (`digiantnz/sovereign-a2a`). Never construct raw dicts — use `A2AMessage.*`.

### Request (sovereign-core → nanobot-01)
```json
{
  "jsonrpc": "3.0",
  "id":      "request_id",
  "method":  "skill-name/operation-name",
  "params":  {"skill": "...", "operation": "...", "payload": {}},
  "metadata": {
    "priority": "normal",
    "stream":   false,
    "context_hints": {"tier": "LOW|MID|HIGH", "retry_strategy": "none|correct_payload", "timeout_ms": 25000}
  }
}
```

### Response — success
```json
{
  "jsonrpc": "3.0",
  "id":      "request_id",
  "result":  {"success": true, "status_code": "HTTP 201|IMAP OK|...", "data": {}},
  "metadata": {
    "agent_card":    {"name": "nanobot-01", "skills": [...], "trust_level": "internal_sidecar"},
    "context_hints": {"execution_path": "dsl|llm"}
  }
}
```

### Response — error
```json
{
  "jsonrpc": "3.0",
  "id":      "request_id",
  "error":   {"code": -32000, "message": "verbatim error", "data": {"skill": "...", "operation": "..."}},
  "metadata": {"context_hints": {"execution_path": "dsl|llm"}}
}
```

Legacy flat format (no `"jsonrpc"` key) is still accepted by `/run` — detected and normalised. All responses are A2A 3.0.

nanobot-01 is a dumb executor: fires the skill, returns protocol result verbatim. No retry, no interpretation, no fabrication.

---

## server.py Dispatch

### Endpoint: `POST /run`
- Accepts raw `Request` body (not Pydantic) — detects format via `"jsonrpc" in body`
- A2A 3.0: parses `method` as `skill/operation`, `params.payload` as operation params, `metadata.stream` (read but ignored for atomic ops)
- Legacy: parses `skill`, `operation|action`, `payload|params`, `context`
- All return paths call `_normalise_to_contract()` → wraps in `A2AMessage.success()` or `A2AMessage.error()`
- `_build_prompt()` uses resolved `operation` and `params`

### `_normalise_to_contract(result, request_id, skill, operation)`
- Derives `success`, `status_code`, `data`, `raw_error` from any dispatcher result format
- Preserves backward-compat fields: `run_id`, `action`, `status`, `path`

### `_dispatch_python3_exec(skill, op_spec, params, run_id, context)`
- Builds path: `workspace/skills/<name>/scripts/<script_rel>`
- Path-traversal guard applied before any file access
- Redeems credential token from `context` via POST to sovereign-core:8000/credential_proxy
- Calls `_dispatch_exec(cmd, run_id, extra_env=credentials)`
- Script must be pre-deployed; error returned if not found

### `_dispatch_exec(cmd, run_id, extra_env=None)`
- Merges `extra_env` into `os.environ.copy()` before `subprocess.run()`
- Used for all script execution

---

## Python3_exec Responses

- Responses are **flat** — script output merged at top level, no nested `"result"` key
- `_forward()` in sovereign-core normalises: if `body.get("result")` is `None`, builds `body_result` from all non-wrapper body fields
- Wrapper fields excluded from normalisation: `{run_id, skill, action, path, elapsed_s}`
- Do NOT use `nb.get("result", nb)` in engine.py — returns `None` when key exists with None value; use: `nb.get("result") if nb.get("result") is not None else nb`

---

## Deployed Scripts

| Script | Location | Skill |
|--------|----------|-------|
| `imap_check.py` | `workspace/skills/imap-smtp-email/scripts/` | imap-smtp-email |
| `smtp_send.py` | `workspace/skills/imap-smtp-email/scripts/` | imap-smtp-email |
| `nextcloud.py` | `workspace/skills/openclaw-nextcloud/scripts/` | openclaw-nextcloud |
| `feeds.py` | `workspace/skills/rss-digest/scripts/` | rss-digest |

Scripts are stdlib/requests Python — no heavy dependencies. Credentials injected via env vars from CredentialProxy.

**Future**: `lifecycle.load()` should auto-deploy `scripts/` directory at skill install time (not yet implemented).

---

## CredentialProxy (sovereign-core side)

- Module: `core/app/execution/credential_proxy.py`
- Services: `imap_business`, `imap_personal`, `smtp_business`, `smtp_personal`, `nextcloud`
- Flow: `CredentialProxy.issue(services)` → UUID token → forwarded in `context` → nanobot-01 redeems via `POST sovereign-core:8000/credential_proxy` → credentials injected as subprocess env vars → immediately invalidated
- Single-use token, 60s TTL

---

## Models

- Ollama model: `llama3.1:8b` (NOT mistral — tools require function calling support)
- Model is on ai_net; nanobot-01 calls Ollama for LLM-path operations

---

### Endpoint: `GET /capabilities`
- Returns `A2AMessage.success()` with `agent_card` in metadata and `result.agent_card`
- sovereign-core calls this on startup via `NanobotAdapter.fetch_capabilities()`
- agent_card lists all DSL-enabled skills from `SKILLS_DIR`

### Error code mapping
| Situation | Code |
|-----------|------|
| Malformed JSON | -32700 |
| Missing `skill` field | -32600 |
| `operation` not in DSL | -32601 |
| Param validation failure | -32602 |
| General execution failure | -32000 |
| Execution timeout | -32001 |
| Skill directory not found | -32002 |

## Common Gotchas

- nanobot-01 `/run` response is flat — `_forward` normalises (see above)
- `SkillLoader._ALWAYS_AVAILABLE` in sovereign-core must include `"nanobot"` — otherwise python3_exec skills are skipped at skill load
- nanobot ledger calls: `self._ledger.append(...)` NOT `.log(...)`
