# Sovereign Security Architecture v2 (ClawSec‑first)

**ClawSec‑Inspired Cognitive Security Model — Version 2.0**  
**Authoritative Scope:** Sovereign Core + Gateway + Specialist Agents  
**Storage:** `/home/sovereign/docs/security-architecturev2.md`

> **Directive:** Prefer *open‑source* functionality from **ClawSec** as the primary method of protection. Where no equivalent exists, fall back to the Sovereign internal controls from v1.0. citeturn1search3

---

## 0. Change Log
- **v2.0**: Integrated ClawSec suite and pre‑execution guardrails; added signed advisory updates, drift auto‑restore, capability separation (agent↔proxy), and governance‑mapped approvals. citeturn1search3turn1search6turn1search12turn1search10

---

## 1. Security Philosophy (Unchanged, operationalized via ClawSec)
Sovereign security remains layered:
- **Deterministic Controls** (hard boundaries)
- **Policy‑Based Tool Gating**
- **Pattern‑Driven Threat Detection** (externalized)
- **Cognitive Risk Evaluation** (Security Persona)
- **Governance‑Enforced Authority Control**
- **Human Escalation** as the final boundary

**ClawSec usage:** Deterministic enforcement is implemented through **ClawSec’s guardrails** (pre‑execution policy engine, ms‑latency) and **suite skills** (integrity & advisories). Security reasoning remains separate in the Security Persona (no execution authority). citeturn1search6turn1search3

> **Non‑delegation principle:** Security is enforced by **architecture + deterministic code + policy files**; LLMs *do not* self‑govern. (Persona only evaluates risk.)

---

## 2. Threat Model (context unchanged)
**Primary Risks:** Prompt injection (direct/indirect), identity override, governance bypass, data exfiltration, tool escalation, destructive operations, external content manipulation, memory poisoning, secret extraction. (ClawSec rulesets directly target destructive commands, secrets, and exfiltration.) citeturn1search6

**High‑Risk Vectors:** email bodies, web content, LLM/Grok responses, file ingestion, user input.

**Supply‑chain note:** Malicious/compromised skills observed in the wild reinforce the need for signed artifacts and drift protection. citeturn1search4

---

## 3. Security Architecture Overview (v2)
```
Inbound Payloads (channels, files, retrieval)
   │
   ▼
[Pre‑LLM Deterministic Scan]
   ├─ patterns: injection/sensitive/policy (Sovereign YAML)
   └─ action: flag→Persona or allow
   │
   ▼
[Security Persona (risk JSON only; no exec)]
   │
   ▼
[Governance Engine (tiers, approvals, rollback checks)]
   │
   ▼
[ClawSec Middleware Intercept  ← NEW]
   ├─ intercepts EVERY tool call (shell/http/file)
   ├─ built‑in & custom rules; action: block/allow/confirm
   └─ <5 ms typical evaluation
   │
   ▼
[Broker / Adapters Execution]
```
ClawSec is the deterministic **pre‑execution gate**; Persona and Governance run *before* it to minimize unnecessary tool attempts. citeturn1search6

---

## 4. Layered Security Model (v2)

### Layer 0 — Infrastructure Hard Controls
- No direct `docker.sock` access; read‑only mounts; no root containers
- Network segmentation (`ai_net` vs `business_net`); loopback binding
- Tier confirmation enforcement; path allowlists; **no secrets exposed to LLM**
- **Capability separation (RECOMMENDED):** Agent has **secrets but no direct network**; MCP/HTTP proxy has **network but no secrets**. Route all agent traffic via the proxy for scanning/logging. citeturn1search10

> These are architectural and **non‑overrideable**.

### Layer 1 — Deterministic Threat Detection
- Externalized pattern files (Sovereign):
  - `/home/sovereign/security/injection_patterns.yaml`
  - `/home/sovereign/security/sensitive_data_patterns.yaml`
  - `/home/sovereign/security/policy_rules.yaml`
- Purpose: flag injections, exfil indicators, authority overrides, destructive intents.
- **ClawSec complement:** prebuilt rulesets for **destructive‑commands**, **secrets/**, **exfiltration/** at the tool layer. citeturn1search6

### Layer 2 — Cognitive Security Agent (Persona)
- Performs contextual analysis and returns **JSON** (`block/risk/mitigation`).
- **No execution**, **no escalation**, **no governance modification**. (Spec unchanged.)

### Layer 3 — Governance Enforcement
- Validates **tier authority**, **rollback strategy**, **domain allowlists**, **confirmation requirements**.
- **ClawSec mapping:** use `action: confirm` with multi‑channel approvals (Slack/Discord/Webhook) for high‑risk actions that need CEO sign‑off. citeturn1search8

### Layer 4 — ClawSec Pre‑Execution Guardrail (NEW)
- Intercepts every tool invocation (shell/HTTP/file I/O) and applies rules in ~milliseconds. 
- Decisions: `block | allow | confirm`; emits structured logs for the ledger.
- Provides open‑source guardrails for agent platforms (OpenClaw/NanoClaw support). citeturn1search6turn1search3

---

## 5. Key Files and Roles (v2)

### 5.1 Sovereign Pattern Files (Pre‑LLM)
- `injection_patterns.yaml` — phrases: identity override, governance bypass, etc.
- `sensitive_data_patterns.yaml` — file paths, keywords for secrets.
- `policy_rules.yaml` — external network allowlist; destructive action rules; memory write approvals.

### 5.2 ClawSec Policy (Pre‑Execution)
- `clawsec.yaml` — **single source** for tool‑level enforcement: 
  - Built‑ins for **destructive‑commands**, **secrets/**, **exfiltration/**; custom regex for injection markers in tool results; approvals via webhook/native; notifications. citeturn1search6turn1search8

### 5.3 Integrity & Signing
- `clawsec-signing-public.pem` — verify suite skill artifacts and updates. citeturn1search3
- **soul‑guardian** skill — drift detection + **auto‑restore** for critical identity/policy files; alert/ignore modes for others. citeturn1search12

---

## 6. Inbound Inspection Flow (Pre‑LLM)
1. Load Sovereign pattern files.  
2. Match categories → if flagged, send to **Security Persona**.  
3. Persona returns `block | sanitize | allow` (JSON only).  
4. If **sanitize**, wrap untrusted content:
   ```
   --- BEGIN UNTRUSTED CONTENT ---
   <external content>
   --- END UNTRUSTED CONTENT ---
   ```
5. Proceed to classification and planning.

> This preserves your v1 behavior and reduces LLM instruction‑following risk from indirect injection. (ClawSec handles *post‑plan* tool risks.) citeturn1search6

---

## 7. Pre‑Execution Inspection Flow (v2)
1. Specialist produces plan/tool intents.  
2. **Governance** checks: destructive intent, sensitive data, domain egress, escalation.  
3. **ClawSec middleware intercepts** the actual tool call with rules (ms‑latency).  
4. Decision path:
   - `block` → emit incident; halt.  
   - `confirm` → route to CEO approver (webhook/native); on approve, proceed.  
   - `allow` → execute via Broker/Adapters.  

ClawSec provides deterministic, open‑source enforcement at the precise moment of risk (the tool boundary). citeturn1search6

---

## 8. Incident Handling Model (v2)
**Security Persona output (unchanged example):**
```json
{
  "block": true,
  "risk_level": "high",
  "risk_categories": ["prompt_injection"],
  "reasoning_summary": "...",
  "required_mitigation": "Strip malicious instruction"
}
```
**ClawSec output (example):**
```
match: "destructive-commands/rm-recursive" → risk: critical → action: block
```
- CEO may: Block, request sanitized reprocessing, or escalate per governance.  
- Alerts/approvals are delivered via Slack/Discord/webhook (config driven). citeturn1search8
- All events are appended to the **audit ledger** (see §12).

---

## 9. Memory Protection Model (v2)
- **Rule:** No external content stored without CEO validation; injection flags preclude storage. (unchanged)
- **ClawSec mapping:** Filesystem rules on `/home/sovereign/memory/**` set to `action: confirm` (tier=high). 
- Memory entries retain: `source, agent, trust_level, timestamp`; vector metadata unchanged.

---

## 10. Outbound Data Protection (v2)
- Before external calls: strip sensitive paths, internal IPs/hostnames; truncate logs; remove tokens.
- **ClawSec mapping:** `exfiltration` rule with `sanitize` options and `network.allow` domain enforcement; default‑deny for egress. citeturn1search6

---

## 11. Security Intelligence & Updates (v2)
- **Source of truth:** ClawSec advisories & suite releases (signed + checksummed). citeturn1search3
- **Process:** Nightly fetch to **Pending** → **Director review** → **Promote to Active**; record in `changelog.md`.  
- **Integrity:** Verify using `clawsec-signing-public.pem`; soul‑guardian monitors for drift and auto‑restores on protected files. citeturn1search3turn1search12

---

## 12. Audit & Governance Ledger (As‑Built)
**Append‑only JSON Lines** with hash‑chaining:
```json
{
  "ts": "2026-03-04T01:25:16Z",
  "agent": "docker",
  "stage": "pre-exec",
  "tool": "bash.run",
  "input_hash": "sha256:...",
  "clawsec": {
    "matched_rules": ["destructive-commands/rm-recursive"],
    "decision": "block",
    "latency_ms": 3.8
  },
  "security_persona": {
    "block": true,
    "risk_level": "critical",
    "risk_categories": ["destructive_action"],
    "reasoning_summary": "Recursive delete on /home detected",
    "required_mitigation": "Disallow; require rollback plan & CEO override"
  },
  "governance": {
    "tier": "high",
    "requires_rollback_plan": true,
    "approval_state": "not_applicable"
  },
  "outcome": "blocked",
  "correlation_id": "ocw-2026-03-04-000123",
  "prev_hash": "sha256:...",
  "record_hash": "sha256:..."
}
```
**Reports:** Daily counts by decision; top matched rules; p95 latency; unapproved egress attempts. (ClawSec provides structured outputs suitable for SIEM ingestion.) citeturn1search6

---

## 13. Security Control Agent — Sovereign Cognitive Firewall (Spec v2)
**Identity & Mission:** *unchanged* — evaluator only; no execution; no governance/tier changes; no memory writes.  
**Scope:** analyzes inbound content, CEO delegation, specialist plans, tool requests, outbound intent, deterministic pattern matches.  
**Output:** strict JSON schema (no conversational text) as in v1.

> Persona runs **in concert** with ClawSec (Persona may recommend policy changes; **cannot** modify `clawsec.yaml` or promote updates directly). citeturn1search12

---

## 14. Operational Runbook (ClawSec‑first)

### 14.1 Suite install (integrity + advisories + drift)
```bash
npx clawhub@latest install clawsec-suite
```
Deploys the suite with integrity verification and skills such as **soul‑guardian** and advisory monitors. citeturn1search15turn1search3

### 14.2 Pre‑execution guardrail (plugin)
```bash
openclaw plugins install clawsec
openclaw plugins info clawsec
openclaw plugins doctor
```
Intercepts shell/HTTP/file tools and enforces rules with `block/allow/confirm` decisions in ~milliseconds. citeturn1search6

### 14.3 OpenClaw plugin config
```yaml
# openclaw.config.yaml
plugins:
  clawsec:
    enabled: true
    configPath: "./clawsec.yaml"
    logLevel: "info"
```
Supports approvals (webhook/native) and notifications (Slack/Discord/Telegram). citeturn1search8

### 14.4 Example `clawsec.yaml` (aligned to Sovereign governance)
```yaml
version: "1.0"

global:
  enabled: true
  logLevel: info
  onError: block

approvals:
  mode: webhook
  webhookUrl: https://<approver-endpoint>/clawsec/approve
  timeoutSeconds: 120

network:
  defaultDecision: deny
  allow:
    - api.x.ai
    - official.documentation
  requireSanitization: true

rules:
  destructive:
    enabled: true
    severity: critical
    action: confirm
    conditions:
      requireRollbackPlan: true
      requireHighTier: true
    patterns:
      - destructive-commands/rm-recursive
      - destructive-commands/chmod-recursive
      - destructive-commands/wipe-docker
      - destructive-commands/k8s-delete-ns

  secrets:
    enabled: true
    severity: critical
    action: block
    filesDeny:
      - "/home/sovereign/secrets/**"
      - "/home/sovereign/memory/**"
      - "/var/run/docker.sock"
    patterns:
      - secrets/api-key
      - secrets/token
      - secrets/private-key
      - secrets/password

  exfiltration:
    enabled: true
    severity: high
    action: confirm
    sanitize:
      stripFilePaths: true
      stripInternalIPs: true
      stripHostnames: true
      truncateLogsKB: 64
    patterns:
      - exfiltration/upload-bulk
      - exfiltration/http-post-large

  prompt_injection:
    enabled: true
    severity: high
    action: block
    patterns:
      - '(?i)(ignore|disregard|forget).{0,60}(instructions|system|previous|prior)'
      - '(?i)(you are now|override your role|disable safeguards)'

  escalation:
    enabled: true
    severity: high
    action: block
    patterns:
      - '(?i)\\brun\\s+shell\\b'
      - '(?i)\\bexecute\\s+system\\s+command\\b'

files:
  - action: write
    paths: ["/home/sovereign/memory/**"]
    decision: confirm
    reason: Memory writes require CEO approval
```
ClawSec provides the **rulesets, approvals, and notifications** to operationalize the above. citeturn1search6turn1search8

### 14.5 Capability Separation Topology (recommended)
```
Agent (secrets, no net)  →  MCP/HTTP Proxy (net, no secrets)  →  Internet
```
This separation reduces exfiltration blast radius even under compromise. citeturn1search10

---

## 15. Fallbacks (when no ClawSec equivalent exists)
- Continue to use Sovereign’s pre‑LLM pattern scan and Persona gating (v1). 
- Keep governance as the final deterministic authority. 
- Document any local custom rule as a candidate for upstream contribution to ClawSec (prefer open‑source convergence).

---

## 16. Final Outcome (v2)
- Deterministic **hard boundaries** and **pre‑execution guardrails** (open‑source). citeturn1search6
- **Integrity verification & drift auto‑restore** for critical identity/policy files. citeturn1search12
- **Signed advisory updates** with Director promotion workflow. citeturn1search3
- **Capability separation** that reduces exfiltration risk. citeturn1search10
- Persona remains a **risk evaluator only**; governance remains final authority; *no security delegation to LLMs*.

---

### References
- ClawSec Suite (GitHub + Docs): capabilities, signed artifacts, skills, advisories. citeturn1search3  
- ClawSec Guardrails (ms‑latency pre‑execution, built‑in rulesets): destructive/secrets/exfiltration. citeturn1search6  
- soul‑guardian (restore/alert/ignore modes; diff/patching; alerts & approval): citeturn1search12  
- Capability Separation pattern for OpenClaw gateways (agent no‑net, proxy net‑only): citeturn1search10  
- Threat context: malicious skills & supply‑chain risks in OpenClaw ecosystems. citeturn1search4
