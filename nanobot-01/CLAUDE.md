# nanobot-01 — Implementation Invariants

This file is loaded by Claude Code when working inside `nanobot-01/`. It supplements the root `CLAUDE.md`.

---

## Role and Boundary

- nanobot-01 is the primary skill execution environment for all application-level skills
- **Hard boundary**: docker-broker handles ONLY system calls (SYSTEM_COMMANDS whitelist); nanobot-01 handles all application skills (IMAP/SMTP/WebDAV/CalDAV/feeds/python3 scripts)
- Container: ai_net + business_net (business_net required for nextcloud.py → `http://nextcloud`)
- Port: 8080 on ai_net

---

## Protocol Contract

### Request (sovereign-core → nanobot-01)
```json
{
  "skill":      "skill-name",
  "operation":  "operation-name",
  "payload":    {},
  "request_id": "uuid",
  "timeout_ms": 25000
}
```
Legacy fields (`action`, `params`, `context`) accepted for backward compat.

### Response (nanobot-01 → sovereign-core)
```json
{
  "request_id":  "uuid",
  "skill":       "skill-name",
  "operation":   "operation-name",
  "success":     true,
  "status_code": "HTTP 201 | IMAP OK | 404 | BAD | ...",
  "data":        {},
  "raw_error":   null
}
```
nanobot-01 is a dumb executor: fires the skill, returns protocol result verbatim. No retry logic, no interpretation, no fabrication.

---

## server.py Dispatch

### Endpoint: `POST /run`
- Resolves: `operation = req.operation or req.action`; `params = req.payload or req.params`
- Calls `_normalise_to_contract()` on all return paths
- `_build_prompt()` uses resolved `operation` and `params` (not raw `req.action`/`req.params`)

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

## Common Gotchas

- nanobot-01 `/run` response is flat — `_forward` normalises (see above)
- `SkillLoader._ALWAYS_AVAILABLE` in sovereign-core must include `"nanobot"` — otherwise python3_exec skills are skipped at skill load
- nanobot ledger calls: `self._ledger.append(...)` NOT `.log(...)`
