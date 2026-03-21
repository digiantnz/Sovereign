#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Sovereign integration test suite
# Usage: ./scripts/test_sovereign.sh [--creds] [--section SECTION]
#
# Flags:
#   --creds           Run credential-dependent tests (mail, nextcloud, browser)
#   --section NAME    Run only one section: infra|govern|docker|memory|mail|
#                                           nextcloud|browser|skills|auth
#
# All tests POST to /chat (the Director interface) unless otherwise noted.
# MID-tier tests stop at requires_confirmation — they do NOT auto-confirm.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BASE_URL="http://localhost:8000"
PASS=0
FAIL=0
SKIP=0
RUN_CREDS=false
SECTION=""

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --creds) RUN_CREDS=true ;;
    --section) SECTION="$2"; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
  shift
done

# ── Helpers ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'; BOLD='\033[1m'

section() {
  echo ""
  echo -e "${BOLD}══ $1 ══${NC}"
}

chat() {
  # chat INPUT [EXTRA_JSON]
  local input="$1"
  local extra="${2:-}"
  if [[ -n "$extra" ]]; then
    echo "{\"input\": $(jq -Rn --arg v "$input" '$v'), $extra}"  | \
      curl -sf -X POST "$BASE_URL/chat" -H "Content-Type: application/json" -d @-
  else
    curl -sf -X POST "$BASE_URL/chat" \
      -H "Content-Type: application/json" \
      -d "{\"input\": $(jq -Rn --arg v "$input" '$v')}"
  fi
}

# assert LABEL RESPONSE CHECKS...
# CHECKS can be:  key=VALUE  (jq field equals string)
#                 has:key    (jq field exists and is not null/false)
#                 not:VALUE  (director_message does not contain string)
#                 mid:       (requires_confirmation is true, no error)
assert() {
  local label="$1"
  local response="$2"
  shift 2
  local ok=true
  local reason=""

  # Basic: response must be valid JSON
  if ! echo "$response" | jq . >/dev/null 2>&1; then
    echo -e "${RED}FAIL${NC} $label — not valid JSON: ${response:0:80}"
    FAIL=$((FAIL+1))
    return
  fi

  for check in "$@"; do
    if [[ "$check" == "has:"* ]]; then
      local key="${check#has:}"
      local val
      val=$(echo "$response" | jq -r ".$key // empty")
      if [[ -z "$val" ]]; then
        ok=false; reason="missing field '$key'"
      fi
    elif [[ "$check" == "not:"* ]]; then
      local needle="${check#not:}"
      local dm
      dm=$(echo "$response" | jq -r '.director_message // ""')
      if echo "$dm" | grep -qi "$needle"; then
        ok=false; reason="director_message contains '$needle'"
      fi
    elif [[ "$check" == "mid:" ]]; then
      local rc
      rc=$(echo "$response" | jq -r '.requires_confirmation // false')
      if [[ "$rc" != "true" ]]; then
        ok=false; reason="expected requires_confirmation=true, got: $rc"
      fi
      # Also must not have an error
      local err
      err=$(echo "$response" | jq -r '.error // empty')
      if [[ -n "$err" ]]; then
        ok=false; reason="has error: $err"
      fi
    elif [[ "$check" == *"="* ]]; then
      local key="${check%%=*}"
      local expected="${check#*=}"
      local actual
      actual=$(echo "$response" | jq -r ".$key // empty")
      if [[ "$actual" != "$expected" ]]; then
        ok=false; reason="$key: expected '$expected', got '$actual'"
      fi
    fi
  done

  if $ok; then
    echo -e "${GREEN}PASS${NC} $label"
    PASS=$((PASS+1))
  else
    echo -e "${RED}FAIL${NC} $label — $reason"
    echo "     Response: $(echo "$response" | jq -c '{dm: .director_message, rc: .requires_confirmation, err: .error} | with_entries(select(.value != null))')"
    FAIL=$((FAIL+1))
  fi
}

skip() {
  echo -e "${YELLOW}SKIP${NC} $1 — $2"
  SKIP=$((SKIP+1))
}

run_section() {
  [[ -z "$SECTION" || "$SECTION" == "$1" ]]
}

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: infra — Health, metrics, direct endpoints
# ═══════════════════════════════════════════════════════════════════════════════
if run_section infra; then
  section "Infrastructure"

  r=$(curl -sf "$BASE_URL/health")
  assert "GET /health — status ok"         "$r" "status=ok"
  assert "GET /health — soul_guardian"     "$r" "soul_guardian=active"
  assert "GET /health — soul_checksum set" "$r" "has:soul_checksum"

  r=$(curl -sf "$BASE_URL/metrics")
  assert "GET /metrics — valid JSON"       "$r" "has:uptime_s"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: govern — Governance validation endpoint
# ═══════════════════════════════════════════════════════════════════════════════
if run_section govern; then
  section "Governance"

  r=$(curl -sf -X POST "$BASE_URL/query" \
    -H "Content-Type: application/json" \
    -d '{"action":{"domain":"docker","operation":"read"},"tier":"LOW"}')
  assert "LOW docker read — allowed"       "$r" "has:docker_read"

  r=$(curl -sf -X POST "$BASE_URL/query" \
    -H "Content-Type: application/json" \
    -d '{"action":{"domain":"file","operation":"write"},"tier":"MID"}')
  assert "MID file write — allowed"        "$r" "has:file_write"

  r=$(curl -sf -X POST "$BASE_URL/query" \
    -H "Content-Type: application/json" \
    -d '{"action":{"domain":"file","operation":"delete"},"tier":"LOW"}' 2>/dev/null || echo '{"detail":"error"}')
  assert "LOW file delete — blocked"       "$r" "has:detail"

  r=$(curl -sf -X POST "$BASE_URL/query" \
    -H "Content-Type: application/json" \
    -d '{"action":{"domain":"browser_config","operation":"configure_auth"},"tier":"MID"}')
  assert "MID browser_config configure_auth — allowed" "$r" "has:file_write"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: docker — Container operations via broker
# ═══════════════════════════════════════════════════════════════════════════════
if run_section docker; then
  section "Docker / Broker (LOW tier)"

  r=$(chat "what containers are currently running")
  assert "docker ps — has director_message"  "$r" "has:director_message"
  assert "docker ps — no error"             "$r" "not:error"

  r=$(chat "show me the last 20 lines of sovereign-core logs")
  assert "docker logs — has director_message" "$r" "has:director_message"

  r=$(chat "show container stats")
  assert "docker stats — has director_message" "$r" "has:director_message"

  # Restart is MID — should require confirmation
  r=$(chat "restart gateway")
  assert "docker restart — requires confirmation" "$r" "mid:"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: memory — Scheduler and memory reads
# ═══════════════════════════════════════════════════════════════════════════════
if run_section memory; then
  section "Scheduler / Memory (LOW tier)"

  r=$(chat "list my scheduled tasks")
  assert "list tasks — has director_message" "$r" "has:director_message"

  r=$(chat "what was the morning briefing yesterday")
  assert "recall briefing — has director_message" "$r" "has:director_message"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: mail — IMAP/SMTP via nanobot-01 (requires credentials)
# ═══════════════════════════════════════════════════════════════════════════════
if run_section mail; then
  section "Mail (IMAP/SMTP)"
  if ! $RUN_CREDS; then
    skip "mail tests" "pass --creds to enable"
  else
    r=$(chat "check my business inbox")
    assert "business IMAP list — has director_message" "$r" "has:director_message"
    assert "business IMAP list — no error"            "$r" "not:failed"

    r=$(chat "check my personal email")
    assert "personal IMAP list — has director_message" "$r" "has:director_message"

    r=$(chat "list my business mailbox folders")
    assert "list_folders business — has director_message" "$r" "has:director_message"

    r=$(chat "search my business email for subject:invoice")
    assert "search business email — has director_message" "$r" "has:director_message"

    # MID — send should stop at confirmation
    r=$(chat "send an email to test@example.com subject 'Sovereign test' body 'This is a test'")
    assert "send email — requires confirmation" "$r" "mid:"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: nextcloud — WebDAV/CalDAV via nanobot-01 (requires credentials)
# ═══════════════════════════════════════════════════════════════════════════════
if run_section nextcloud; then
  section "Nextcloud (WebDAV / CalDAV)"
  if ! $RUN_CREDS; then
    skip "nextcloud tests" "pass --creds to enable"
  else
    r=$(chat "list my Nextcloud files")
    assert "webdav list root — has director_message" "$r" "has:director_message"
    assert "webdav list root — no error"            "$r" "not:failed"

    r=$(chat "list my calendar events")
    assert "caldav list events — has director_message" "$r" "has:director_message"

    r=$(chat "list my tasks")
    assert "caldav list tasks — has director_message" "$r" "has:director_message"

    # Create event is MID
    r=$(chat "schedule a meeting called 'Test event' for tomorrow at 2pm")
    assert "create event — requires confirmation" "$r" "mid:"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: browser — a2a-browser search and fetch (requires node04 reachable)
# ═══════════════════════════════════════════════════════════════════════════════
if run_section browser; then
  section "Browser (a2a-browser / node04)"
  if ! $RUN_CREDS; then
    skip "browser tests" "pass --creds to enable (also requires node04 reachable)"
  else
    r=$(chat "search the web for 'New Zealand weather today'")
    assert "browser search — has director_message" "$r" "has:director_message"
    assert "browser search — no hard error"       "$r" "not:unreachable"

    r=$(chat "fetch the content of https://wttr.in/?format=3")
    assert "browser fetch — has director_message" "$r" "has:director_message"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: skills — Skill search and lifecycle
# ═══════════════════════════════════════════════════════════════════════════════
if run_section skills; then
  section "Skills"

  # skill_search is LOW — always runnable; GITHUB_PAT may be blank
  r=$(chat "search for skills that can read RSS feeds")
  assert "skill search RSS — has director_message" "$r" "has:director_message"

  # skill_audit is LOW
  r=$(chat "audit all installed skills")
  assert "skill audit — has director_message" "$r" "has:director_message"

  # skill_install is composite — starts with search (LOW), load is MID
  GITHUB_PAT_SET=$(grep -E "^GITHUB_PAT=.+" /home/sovereign/sovereign/secrets/browser.env 2>/dev/null || true)
  if [[ -z "$GITHUB_PAT_SET" ]]; then
    skip "skill_install via GitHub" "GITHUB_PAT not set in secrets/browser.env"
  else
    r=$(chat "install the skill at https://github.com/digiant-ai/sovereign-skills/tree/main/weather")
    assert "skill install search step — has director_message" "$r" "has:director_message"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION: auth — Browser auth profile configuration (MID)
# ═══════════════════════════════════════════════════════════════════════════════
if run_section auth; then
  section "Browser Auth Profile (configure_browser_auth)"

  # Should route to configure_browser_auth and fire MID confirmation
  r=$(chat "configure browser auth for api.test.example.com bearer token TEST_API_KEY")
  assert "configure_browser_auth — requires confirmation" "$r" "mid:"
  assert "configure_browser_auth — no error"             "$r" "not:error"

  # Confirm the write
  PENDING=$(echo "$r" | jq -c '.pending_delegation // empty')
  if [[ -n "$PENDING" ]]; then
    r2=$(curl -sf -X POST "$BASE_URL/chat" \
      -H "Content-Type: application/json" \
      -d "{\"input\": \"yes\", \"confirmed\": true, \"pending_delegation\": $PENDING}")
    assert "configure_browser_auth confirmed — profile written" "$r2" "has:director_message"
    assert "configure_browser_auth confirmed — no error"       "$r2" "not:failed"

    # Clean up test entry from YAML
    python3 -c "
import yaml, copy
path = '/home/sovereign/governance/browser-auth-profiles.yaml'
with open(path) as f: data = yaml.safe_load(f)
data.setdefault('profiles', {}).pop('api.test.example.com', None)
with open(path, 'w') as f: yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
print('Cleaned up test entry')
" 2>/dev/null || echo "  (cleanup skipped — check yaml manually)"
  else
    skip "configure_browser_auth confirm step" "no pending_delegation in response"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo "─────────────────────────────────────────"
TOTAL=$((PASS+FAIL+SKIP))
echo -e "Results: ${GREEN}$PASS passed${NC}  ${RED}$FAIL failed${NC}  ${YELLOW}$SKIP skipped${NC}  ($TOTAL total)"
if [[ $FAIL -gt 0 ]]; then
  echo -e "${RED}SOME TESTS FAILED${NC}"
  exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
