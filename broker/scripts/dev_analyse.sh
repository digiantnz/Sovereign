#!/bin/sh
# Dev-Harness Phase 1 — local analysis tool chain.
#
# Runs pylint, semgrep, and boundary_scanner against the sovereign-core app
# directory on the host filesystem. Emits results as NDJSON to stdout.
# Each tool's output is wrapped in a tool-envelope line so harness.py can
# distinguish sources.
#
# Usage (broker dispatches this via commands-policy.yaml dev_analyse entry):
#   /scripts/dev_analyse.sh
#
# Scan target is always /hostfs/home/sovereign/sovereign/core/app — the broker's
# host filesystem bind mount. No parameters accepted; the target is fixed
# to prevent path traversal.
#
# Output format — one JSON object per line:
#   {"tool":"pylint",   "stdout":"...", "stderr":"...", "exit_code":N}
#   {"tool":"semgrep",  "stdout":"...", "stderr":"...", "exit_code":N}
#   {"tool":"boundary", "stdout":"...", "stderr":"...", "exit_code":N}
#
# Errors in individual tools do NOT abort the script. All three tools always
# run. The script always exits 0.

set -e
SCAN_ROOT="/hostfs/home/sovereign/sovereign/core/app"
SEMGREP_CONFIG="/app/semgrep-rules.yaml"
BOUNDARY_SCRIPT="/scripts/boundary_scanner.py"

# ── helpers ──────────────────────────────────────────────────────────────────

emit() {
    # emit_tool_result TOOL STDOUT STDERR EXIT_CODE
    # Writes a single JSON envelope line. Newlines in stdout/stderr are
    # preserved as \n (jq -Rs handles multi-line raw strings).
    tool="$1"
    stdout="$2"
    stderr="$3"
    exit_code="$4"
    printf '%s\n' "$stdout" | jq -Rs --arg tool "$tool" \
        --arg stderr "$stderr" \
        --argjson exit_code "$exit_code" \
        '{tool: $tool, stdout: ., stderr: $stderr, exit_code: $exit_code}'
}

# ── pylint ────────────────────────────────────────────────────────────────────
pylint_stdout=""
pylint_stderr=""
pylint_exit=0

if command -v python3 >/dev/null 2>&1 && python3 -m pylint --version >/dev/null 2>&1; then
    pylint_stdout=$(python3 -m pylint --output-format=json "$SCAN_ROOT" 2>/tmp/pylint_err || true)
    pylint_stderr=$(cat /tmp/pylint_err 2>/dev/null || true)
    # pylint exits non-zero when findings exist — that is expected
    pylint_exit=0
else
    pylint_stderr="pylint not available in broker container"
    pylint_exit=1
fi

emit "pylint" "$pylint_stdout" "$pylint_stderr" "$pylint_exit"

# ── semgrep ───────────────────────────────────────────────────────────────────
semgrep_stdout=""
semgrep_stderr=""
semgrep_exit=0

if command -v semgrep >/dev/null 2>&1; then
    semgrep_stdout=$(semgrep --config "$SEMGREP_CONFIG" --json "$SCAN_ROOT" 2>/tmp/semgrep_err || true)
    semgrep_stderr=$(cat /tmp/semgrep_err 2>/dev/null || true)
    semgrep_exit=0
else
    semgrep_stderr="semgrep not available in broker container"
    semgrep_exit=1
fi

emit "semgrep" "$semgrep_stdout" "$semgrep_stderr" "$semgrep_exit"

# ── boundary scanner ──────────────────────────────────────────────────────────
boundary_stdout=""
boundary_stderr=""
boundary_exit=0

if [ -f "$BOUNDARY_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
    boundary_stdout=$(python3 "$BOUNDARY_SCRIPT" "$SCAN_ROOT" 2>/tmp/boundary_err || true)
    boundary_stderr=$(cat /tmp/boundary_err 2>/dev/null || true)
    # boundary_scanner always exits 0 by design
    boundary_exit=0
else
    boundary_stderr="boundary_scanner.py not found at $BOUNDARY_SCRIPT or python3 unavailable"
    boundary_exit=1
fi

emit "boundary" "$boundary_stdout" "$boundary_stderr" "$boundary_exit"

exit 0
