# Sovereign Management Portal — Design & As-Built Reference

**Status:** COMPLETE  
**Built:** 2026-04-01  
**Module:** `core/app/api/portal.py`  
**HTML:** `/home/sovereign/portal/sovereign-portal.html` (RAID, `:ro` mount)  
**as-built entry:** `/home/sovereign/docs/as-built.md` → "Sovereign Management Portal — 2026-04-01"

---

## Purpose

Read-only introspective dashboard for the Director. Surfaces Rex's live state — skills, harnesses, governance policy, memory, and container logs — without exposing any execution or write path.

**Primary invariant:** Portal endpoints are read-only. No sovereign collection writes. No secrets in scope. All reads append to the audit ledger at LOW tier.

---

## Access

| Path | Description |
|------|-------------|
| `http://172.16.201.25:8000/dashboard` | Portal HTML (LAN) |
| `https://sovereign.tail887d2b.ts.net:8443/dashboard` | Portal HTML (Tailscale) |

Served via `nextcloud-rp` nginx on port 8000. Nginx proxies portal routes to `sovereign-core:8000`; all other paths return 403.

**Nginx resolver note:** `resolver 127.0.0.11 valid=30s ipv6=off` with `set $sovereign http://sovereign-core:8000` is required so nginx resolves the Docker hostname per-request. Without it, nginx caches the `ai_net` IP at startup; `nextcloud-rp` is only on `business_net` and cannot reach that subnet.

---

## Endpoints

All endpoints: `GET`, LOW tier, no authentication, audit-logged, read-only.

### `GET /dashboard`

Serves `sovereign-portal.html` from `/home/sovereign/portal/` (RAID `:ro` mount). Returns 404 JSON if file missing.

### `GET /skills`

Scans `/home/sovereign/skills/*/SKILL.md` at request time (live, not cached). Parses YAML frontmatter via regex `^---\n(.*?)\n---\n(.*)`. Returns per-skill:

```json
{
  "name": "rss-digest",
  "executor": "python3_exec → nanobot-01",
  "specialists": ["research_agent"],
  "tier_required": "LOW",
  "ops": ["get_entries", "add_feed", "remove_feed"],
  "loaded": true
}
```

`loaded` is cross-referenced against `app.state.skill_summary` (populated at startup by `scan_all_skills()`).

Executor is derived from `sovereign.adapter_deps`: if `"nanobot"` in deps → `"python3_exec → nanobot-01"`, otherwise joins deps with `" + "`.

### `GET /harnesses`

Returns harness session state. Two data sources:

1. **working_memory scroll** — unfiltered scroll for harness checkpoint payloads identified by flag field (`_developer_harness_checkpoint`, `_self_improvement_session`, `_skill_harness_checkpoint`). Checkpoint present → `status: "RUNNING"`.
2. **PROSPECTIVE scroll** — dev-harness only: identifies nightly task by step `intent=dev_analyse + params.trigger=nightly`. Supplies `last_run`, `next_due`, `status`.

Status values: `RUNNING` (active checkpoint in WM) | `SCHEDULED` (dev-harness PROSPECTIVE active) | `PLANNED` (static manifest, not yet run).

Static harness manifest (`HARNESS_DEFS`) is the source of truth for phases, trigger, and HITL flag. PM-Harness is listed with `flag: None` (PLANNED — not yet implemented).

Last-run timestamp: dev-harness uses `last_observe_ts`; others use `last_checkpoint_ts` from the WM checkpoint payload.

### `GET /governance`

Returns parsed `governance.json` verbatim from `/app/governance/governance.json` (RAID `:ro` mount). No transformation. 404 if file missing.

### `GET /memory/preview?collection=<name>&limit=<1-50>`

Scrolls a single RAID sovereign collection via `archive_client.scroll()`. Returns top N points (no vector ranking — scroll order only). Content fields truncated to 200 chars at API layer.

Valid collections: `semantic`, `episodic`, `prospective`, `procedural`, `associative`, `relational`, `meta`. Returns 400 on unknown collection name.

```json
{
  "collection": "semantic",
  "points": [
    {"id": "abc123", "score": null, "payload": {"content": "...", "_key": "..."}}
  ]
}
```

**TODO:** add `?query=<text>` parameter for vector-ranked preview via `archive_client.search()`.

### `GET /logs/stream`

Server-Sent Events (SSE) endpoint. Opens three parallel streaming connections to the broker Docker API proxy (`GET /containers/{name}/logs`), one per container: `sovereign-core`, `gateway`, `nanobot-01`.

Lines are merged into a shared `asyncio.Queue(maxsize=200)` and yielded as `data: [container] line\n\n` SSE events. A 25-second heartbeat (`data: [heartbeat]`) keeps the connection alive through proxies.

Docker multiplexed log stream parser: 8-byte header (byte 0 = stream type, bytes 4–7 = big-endian uint32 frame size), followed by frame payload. Both stdout and stderr are surfaced (stream type is discarded).

All tasks are cancelled on client disconnect. Permitted via `docker-policy.yaml` named intent `log_tail_sovereign`.

---

## File Locations

| File | Role |
|------|------|
| `core/app/api/portal.py` | All portal API endpoints |
| `/home/sovereign/portal/sovereign-portal.html` | Dashboard HTML (RAID `:ro`) |
| `nginx/nextcloud.conf` | Port 8000 proxy block |
| `compose.yml` | `:ro` mount + port 8000 binding on nextcloud-rp |

`portal_router` is imported and registered in `main.py`:
```python
from api.portal import router as portal_router
app.include_router(portal_router)
```

---

## Router Registration

`portal_router` must be registered with `app.include_router(portal_router)` in `main.py`. Without this the routes exist in `portal.py` but the FastAPI app never serves them — all requests return 502 via nginx.

---

## Nginx Proxy Architecture

```
Director browser
  → 172.16.201.25:8000 (host port)
    → nextcloud-rp:8000 (nginx container)
      → location ~* ^/(dashboard|metrics|skills|harnesses|governance|memory/preview)
        → proxy_pass $sovereign (http://sovereign-core:8000, resolved per-request)
          → portal.py route handler
```

`/chat` and all other sovereign-core routes are blocked (403) at the nginx layer. The portal is read-only by nginx policy, not just by code.

---

## Security Model

- **No authentication.** LAN-only access via 172.16.201.25. Tailscale access requires the Director's device to be on the tailnet.
- **No writes.** All portal endpoints are GET. No Qdrant writes, no file writes, no exec calls.
- **Audit-logged.** Every endpoint appends to the `AuditLedger` at LOW tier with label `portal_read`.
- **Log stream** uses the broker's existing `GET /containers/*/logs` whitelist — no new broker permissions required.
- **Governance.json** served as-is — contains no secrets (API keys are in `secrets/` env files, not governance).

---

## Design Decisions

**RAID-mounted HTML, not embedded.** The portal HTML lives at `/home/sovereign/portal/sovereign-portal.html` mounted `:ro` into sovereign-core. This means the Director can update the dashboard UI without rebuilding the container.

**Scan at request time, not cache.** `/skills` scans SKILL.md files on every request. Stale cache would misrepresent the loaded skill set after a `/install` or manual skill update. The RAID directory is small; scan cost is negligible.

**WM-backed harness state, not EPISODIC.** Harness checkpoints in working_memory are the live session state. EPISODIC entries are the historical record. The portal shows current state (WM), not history.

**SSE for logs, not polling.** Container logs stream continuously via SSE. The client doesn't poll; the server pushes. The 25s heartbeat prevents proxy/firewall timeout on idle streams.

**Separate resolver required.** `nextcloud-rp` is on `business_net` only. `sovereign-core` is dual-homed (`ai_net` + `business_net`). Without `resolver 127.0.0.11` and a `set $var` upstream, nginx resolves DNS once at startup and caches the `ai_net` IP — which `nextcloud-rp` cannot reach. Per-request DNS resolution via Docker's embedded resolver (`127.0.0.11`) is mandatory.
