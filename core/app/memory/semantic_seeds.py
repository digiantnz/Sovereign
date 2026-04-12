"""Static definitions for intent and skill semantic memory seeds.

Written to the SEMANTIC Qdrant collection at startup (idempotent).
Each entry: _key = semantic:intent:{slug}, pointing to the canonical
trigger location for that intent, skill, or harness.

build_intent_seeds()  — one entry per INTENT_ACTION_MAP key
build_skill_seeds()   — one entry per installed RAID skill (v2: rich I/O content)
build_harness_seeds() — one entry per whole harness system
make_skill_semantic_seed() — called by lifecycle.load() on new skill install
"""

import os
import re
import yaml
from datetime import datetime, timezone

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower().strip()).strip("-")[:48]


# Domain → (owner, trigger_point)
_DOMAIN_META: dict[str, tuple[str, str]] = {
    "docker":            ("engine",  "engine:broker_adapter"),
    "webdav":            ("nanobot", "nanobot:sovereign-nextcloud-fs"),
    "caldav":            ("nanobot", "nanobot:openclaw-nextcloud"),
    "notes":             ("nanobot", "nanobot:openclaw-nextcloud"),
    "ncfs":              ("nanobot", "nanobot:sovereign-nextcloud-fs"),
    "ncingest":          ("nanobot", "nanobot:sovereign-nextcloud-ingest"),
    "session":           ("engine",  "engine:cognitive_skill"),
    "memory_curate":     ("engine",  "engine:cognitive_skill"),
    "mail":              ("nanobot", "nanobot:nc-mail"),
    "ollama":            ("engine",  "engine:cognition_module"),
    "browser":           ("nanobot", "nanobot:sovereign-browser"),
    "feeds":             ("nanobot", "nanobot:rss-digest"),
    "security":          ("engine",  "engine:github_adapter"),
    "github":            ("engine",  "engine:github_adapter"),
    "skills":            ("harness", "harness:skill_lifecycle"),
    "memory":            ("engine",  "engine:qdrant_adapter"),
    "memory_index":      ("engine",  "engine:qdrant_adapter"),
    "memory_synthesise": ("bespoke", "bespoke:memory/synthesis.py"),
    "wallet":            ("engine",  "engine:wallet_adapter"),
    "wallet_watchlist":  ("engine",  "engine:sov-wallet_service"),
    "scheduler":         ("engine",  "engine:task_scheduler"),
    "nanobot":           ("engine",  "engine:nanobot_adapter"),
    "browser_config":    ("bespoke", "bespoke:engine:browser_config_handler"),
    "monitoring":        ("harness", "harness:self_improvement"),
    "dev_harness":       ("harness", "harness:dev_harness"),
    "portal":            ("engine",  "engine:portal_handler"),
    "tax":               ("harness", "harness:tax_ingest"),
    "tax_report":        ("harness", "harness:tax_report"),
    "news":              ("harness", "harness:news_harness"),
}


def build_intent_seeds(intent_action_map: dict) -> list[dict]:
    """Generate semantic seed dicts for every entry in INTENT_ACTION_MAP."""
    seeds = []
    for intent, action in intent_action_map.items():
        domain    = action.get("domain", "")
        operation = action.get("operation", "")
        name      = action.get("name", "")
        owner, trigger_point = _DOMAIN_META.get(domain, ("engine", f"engine:{domain}"))
        slug    = _slug(intent.replace("_", "-"))
        key     = f"semantic:intent:{slug}"
        content = (
            f"Intent: {intent}. Domain: {domain}. Operation: {operation}. "
            f"Action name: {name or operation}. "
            f"Sovereign capability — dispatches via {trigger_point}."
        )
        seeds.append({
            "seed_id":   f"intent_seed_v1_{intent}",
            "key":       key,
            "title":     f"{intent} — {domain}:{operation}",
            "content":   content,
            "domain":    domain,
            "extra_meta": {
                "intent_signals": [intent.replace("_", " "), intent],
                "action":         f"{domain}:{operation}:{name or operation}",
                "trigger_point":  trigger_point,
                "owner":          owner,
                "success_count":  0,
                "failure_count":  0,
            },
        })
    return seeds


def _parse_skill_md(skill_path: str) -> dict:
    """Parse a SKILL.md file into a structured dict.

    Returns: {name, description, specialists, tier, operations}
    where operations is a list of {name, inputs, returns} dicts.
    Falls back gracefully on any parse error.
    """
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Extract frontmatter block between first two --- delimiters
        fm_match = re.match(r"^---\n(.*?)\n---\n?(.*)", raw, re.DOTALL)
        if not fm_match:
            return {}

        fm_text = fm_match.group(1)
        try:
            fm = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            return {}

        sv = fm.get("sovereign", {}) or {}
        ops_raw = sv.get("operations", {}) or {}

        # Build operations list with I/O descriptions
        operations = []
        if isinstance(ops_raw, dict):
            for op_name, op_def in ops_raw.items():
                if not isinstance(op_def, dict):
                    continue
                # Input: required param names
                params = op_def.get("params", {}) or {}
                required_params = [
                    k for k, v in params.items()
                    if isinstance(v, dict) and v.get("required", False)
                ] if isinstance(params, dict) else []
                optional_params = [
                    k for k, v in params.items()
                    if isinstance(v, dict) and not v.get("required", False)
                ] if isinstance(params, dict) else []

                inputs_str = ""
                if required_params:
                    inputs_str += f"required: {', '.join(required_params)}"
                if optional_params:
                    inputs_str += (
                        ("; " if inputs_str else "") +
                        f"optional: {', '.join(optional_params)}"
                    )

                returns_str = str(op_def.get("returns", "")).strip()
                operations.append({
                    "name":    op_name,
                    "inputs":  inputs_str,
                    "returns": returns_str,
                })

        # Specialists: handle both list and single-value
        specialists_raw = sv.get("specialists", [])
        if isinstance(specialists_raw, str):
            specialists_raw = [specialists_raw]

        return {
            "name":        fm.get("name", ""),
            "description": fm.get("description", ""),
            "specialists": [str(s) for s in (specialists_raw or [])],
            "tier":        sv.get("tier_required", "LOW"),
            "operations":  operations,
        }
    except Exception:
        return {}


def _build_skill_content(skill_name: str, parsed: dict) -> str:
    """Build a rich content string for a skill semantic entry.

    Includes: description, operations with inputs/outputs, specialists, tier.
    Sovereign memory should be the source of truth — enough detail for Rex
    to understand inputs, processing, and outputs without reading SKILL.md.
    """
    parts = [f"Nanobot skill: {skill_name}."]

    description = parsed.get("description", "")
    if description:
        parts.append(f"Description: {description}")

    operations = parsed.get("operations", [])
    if operations:
        op_strs = []
        for op in operations:
            op_str = op["name"]
            if op.get("inputs"):
                op_str += f" (inputs: {op['inputs']}"
                if op.get("returns"):
                    op_str += f" → returns: {op['returns']}"
                op_str += ")"
            elif op.get("returns"):
                op_str += f" (returns: {op['returns']})"
            op_strs.append(op_str)
        parts.append(f"Operations: {'; '.join(op_strs)}.")

    specialists = parsed.get("specialists", [])
    if specialists:
        parts.append(f"Active for specialists: {', '.join(specialists)}.")

    tier = parsed.get("tier", "LOW")
    parts.append(f"Governance tier: {tier}.")
    parts.append(
        f"Installed at /home/sovereign/skills/{skill_name}/SKILL.md. "
        "Executed by nanobot-01 via python3_exec or DSL operations."
    )

    return " ".join(parts)


def build_skill_seeds(skills_dir: str = "/home/sovereign/skills") -> list[dict]:
    """Generate semantic seed dicts for all installed RAID skills.

    v2: parses SKILL.md frontmatter to include description, operations with
    inputs/outputs, specialists, and tier in the content field. This makes
    semantic memory the source of truth — Rex can answer "what does skill X do
    and what are its inputs/outputs?" without reading raw SKILL.md files.

    _prev_seed_id is included so seed_intent_semantic_entries() can clean up
    the sparse v1 entry on first v2 write.
    """
    seeds = []
    if not os.path.isdir(skills_dir):
        return seeds
    for skill_name in sorted(os.listdir(skills_dir)):
        skill_path = os.path.join(skills_dir, skill_name, "SKILL.md")
        if not os.path.isfile(skill_path):
            continue
        slug   = _slug(skill_name)
        key    = f"semantic:intent:{slug}"
        parsed = _parse_skill_md(skill_path)

        seeds.append({
            "seed_id":       f"skill_seed_v2_{skill_name}",
            "_prev_seed_id": f"skill_seed_v1_{skill_name}",
            "key":           key,
            "title":         f"Skill: {skill_name}",
            "content":       _build_skill_content(skill_name, parsed),
            "domain":        "skills",
            "extra_meta": {
                "intent_signals":  [skill_name, skill_name.replace("-", " ")],
                "action":          f"nanobot:skill:{skill_name}",
                "trigger_point":   f"nanobot:{skill_name}",
                "owner":           "nanobot",
                "operations":      [op["name"] for op in parsed.get("operations", [])],
                "specialists":     parsed.get("specialists", []),
                "tier":            parsed.get("tier", "LOW"),
                "success_count":   0,
                "failure_count":   0,
            },
        })
    return seeds


def build_harness_seeds() -> list[dict]:
    """Generate semantic seed dicts for all harness systems."""
    harnesses = [
        {
            "name": "skill-harness",
            "description": (
                "Multi-step skill lifecycle harness in execution/engine.py. "
                "Steps: search → list_candidates → review_candidate → install → clear. "
                "Inputs: goal (search query or GitHub URL). "
                "Processing: GitHub skill search, pre-scan security review, Director confirm gate. "
                "Output: skill installed to /home/sovereign/skills/ + semantic entry written. "
                "Manages skill discovery with working_memory checkpointing."
            ),
            "trigger_point": "harness:skill_lifecycle",
        },
        {
            "name": "SI-harness",
            "description": (
                "Self-improvement harness in monitoring/self_improvement.py. "
                "Inputs: system metrics (CPU, memory, error rates). "
                "Processing: daily observe loop, baseline and anomaly detection, "
                "proposal generation with Director approval gate. "
                "Output: approved proposals written to PROSPECTIVE; never self-modifies. "
                "Primary autonomy boundary."
            ),
            "trigger_point": "harness:self_improvement",
        },
        {
            "name": "dev-harness",
            "description": (
                "4-phase code quality harness in dev_harness module. "
                "Inputs: sovereign-core source tree. "
                "Processing: Analyse (pylint+semgrep+boundary_scanner) → Classify (Ollama/Claude) "
                "→ Plan (Director notification) → Execute (CC runsheet HITL handoff). "
                "Output: structured CC runsheet with findings, suggested fixes, acceptance criteria. "
                "Never self-modifies. Nightly cron at 15:00 UTC (03:00 NZST)."
            ),
            "trigger_point": "harness:dev_harness",
        },
        {
            "name": "tax-ingest-harness",
            "description": (
                "Continuous tax ingestion pipeline in tax_harness/harness.py. "
                "Inputs: Nextcloud /Digiant/Tax/ CSV/PDF files; on-chain push events from "
                "wallet watcher via /wallet_event endpoint. "
                "Processing: list unprocessed Nextcloud files → parse CSV (Wirex/Swyftx format "
                "auto-detected) or PDF receipts → enrich with NZD value via CoinGecko → write "
                "TaxEvent records to semantic memory (UUID5 deterministic dedup). "
                "Two event tags: tax:crypto (on-chain and exchange trades), "
                "tax:expense (card spends, receipts, invoices). "
                "No classification at ingest time — all tax treatment deferred to /do_tax. "
                "Session flag: _tax_ingest_harness_checkpoint. "
                "Cron: 0 * * * * (hourly UTC). Status: pending_approval until Director activates."
            ),
            "trigger_point": "harness:tax_ingest",
        },
        {
            "name": "tax-report-harness",
            "description": (
                "NZ tax report generator in tax_harness/report_harness.py. "
                "Triggered by /do_tax [year] Telegram command. "
                "Three-turn human-in-the-loop flow: "
                "(1) queries semantic memory by date range for all tax events in the requested "
                "NZ financial year (01 Apr YYYY-1 – 31 Mar YYYY); classifies tax:crypto events; "
                "reports counts; asks Director for supplementary expense CSV filenames. "
                "(2) parses named CSVs from Nextcloud (not stored to memory); merges with "
                "memory expense records; reports in-scope row counts; asks for confirmation. "
                "(3) generates income{year}.csv and expenses{year}.csv in "
                "/Digiant/Tax/FY{year}/ via Nextcloud. "
                "Classifier labels: staking_reward, exchange_acquisition, exchange_disposal, "
                "internal_transfer, unknown_inbound, unknown_outbound, unknown. "
                "FIFO disposal calculations deferred to Phase 3. "
                "Prerequisites: semantic:tax:taxable_wallets and semantic:tax:staking_contracts "
                "must be populated by Director for accurate classification. "
                "Session flag: _tax_report_harness_checkpoint."
            ),
            "trigger_point": "harness:tax_report",
        },
    ]
    seeds = []
    for h in harnesses:
        slug = _slug(h["name"])
        key  = f"semantic:intent:{slug}"
        seeds.append({
            "seed_id":   f"harness_seed_v1_{h['name']}",
            "key":       key,
            "title":     f"Harness: {h['name']}",
            "content":   h["description"],
            "domain":    "harness",
            "extra_meta": {
                "intent_signals": [h["name"], h["name"].replace("-", " ")],
                "action":         f"harness:{h['name']}",
                "trigger_point":  h["trigger_point"],
                "owner":          "harness",
                "success_count":  0,
                "failure_count":  0,
            },
        })
    return seeds


def build_tax_address_seeds() -> list[dict]:
    """Placeholder semantic entries for the Director-populated tax address lists.

    These entries exist so the tax harness can query them at startup.
    Content is populated by the Director via Rex — the harness reports
    current contents at first run and requests population if empty.

    Keys:
      semantic:tax:taxable_wallets   — ETH addresses that generate taxable events
      semantic:tax:staking_contracts — known staking contract addresses (used by /do_tax
                                       at report time to classify inbound as staking rewards)

    Note: semantic:tax:internal_addresses is NOT seeded here.
    Whether a transaction is internal is determined at report time by /do_tax
    comparing addresses against semantic memory — never at ingest time.
    """
    return [
        {
            "seed_id": "tax_seed_v1_taxable_wallets",
            "key":     "semantic:tax:taxable_wallets",
            "title":   "Tax: Taxable wallet addresses",
            "content": (
                "List of ETH wallet addresses watched for tax purposes. "
                "The wallet watcher uses this list to decide which on-chain transactions to push "
                "to the /wallet_event endpoint. Every event the wallet watcher pushes is stored "
                "as a tax:crypto event — no further filtering by the harness. "
                "Also used by /do_tax at report time to identify which side of a transaction "
                "belongs to the Director. "
                "Director must populate this list. Current entries: [] (empty — awaiting Director)."
            ),
            "domain": "tax",
            "extra_meta": {
                "addresses":      [],
                "populated":      False,
                "intent_signals": ["taxable wallets", "tax wallets"],
                "owner":          "director",
            },
        },
        {
            "seed_id": "tax_seed_v1_staking_contracts",
            "key":     "semantic:tax:staking_contracts",
            "title":   "Tax: Known staking contract addresses",
            "content": (
                "List of known staking contract addresses (e.g. Rocket Pool deposit pool, "
                "Rocket Pool node operator). Used by /do_tax at report time to classify "
                "inbound ETH from these addresses as staking income. "
                "Director must populate this list. Current entries: [] (empty — awaiting Director)."
            ),
            "domain": "tax",
            "extra_meta": {
                "addresses":      [],
                "populated":      False,
                "intent_signals": ["staking contracts", "rocket pool"],
                "owner":          "director",
            },
        },
    ]


def build_crypto_domain_seeds() -> list[dict]:
    """Foundational knowledge seeds for cryptocurrency assets and address formats.

    Gives Rex:
    - Recognition of ETH and BTC address formats from raw strings
    - Basic understanding of each asset and its NZ tax treatment
    - Vocabulary to reason about crypto transactions (staking, disposal, acquisition)

    Seeds are idempotent — re-seeded on each startup, safe to update in place.
    """
    return [
        {
            "seed_id": "crypto_seed_v1_ethereum",
            "key":     "semantic:domain:cryptocurrency:ethereum",
            "title":   "Ethereum (ETH) — asset definition, address format, NZ tax treatment",
            "content": (
                "Ethereum (ETH) is a proof-of-stake blockchain and cryptocurrency. "
                "ETH wallet addresses are 42-character strings starting with 0x followed by 40 hexadecimal characters "
                "(e.g. 0x2c228a2d04d65E54dE6b24885C1D3626098C776e). "
                "All Digiant ETH addresses follow this format. "
                "ERC-20 token addresses (e.g. USDC, WETH, stablecoins) use the same format. "
                "ETH staking rewards (from Rocket Pool node operation or eth-docker) are taxable income in NZ "
                "at the NZD market value on the date received. "
                "Disposal of ETH (sale, swap, or exchange for fiat) is a taxable event — "
                "gain or loss calculated as proceeds minus cost basis (FIFO method). "
                "Internal transfers between wallets you own are not taxable events. "
                "Depositing ETH to an exchange you own (e.g. Wirex, Swyftx) is not a disposal. "
                "Asset symbol: ETH. Decimal places: 18. Chain: Ethereum mainnet (chain ID 1)."
            ),
            "domain": "cryptocurrency",
            "extra_meta": {
                "asset":          "ETH",
                "chain":          "ethereum",
                "address_prefix": "0x",
                "address_length": 42,
                "address_regex":  r"0x[0-9a-fA-F]{40}",
                "nz_tax_events":  ["staking_reward", "disposal", "acquisition"],
                "intent_signals": ["ethereum", "ETH", "eth address", "ether"],
            },
        },
        {
            "seed_id": "crypto_seed_v1_bitcoin",
            "key":     "semantic:domain:cryptocurrency:bitcoin",
            "title":   "Bitcoin (BTC) — asset definition, address formats, NZ tax treatment",
            "content": (
                "Bitcoin (BTC) is a proof-of-work cryptocurrency and the first blockchain. "
                "BTC addresses come in three formats: "
                "Legacy (P2PKH): starts with 1, 25–34 characters (e.g. 1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf NA). "
                "Script (P2SH): starts with 3, 34 characters (e.g. 3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy). "
                "Native SegWit (bech32): starts with bc1, 42 characters (e.g. bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq). "
                "Digiant runs a Bitcoin node (BTC RPC endpoint in network endpoints). "
                "BTC disposal (sale, swap, or exchange for fiat) is a taxable event in NZ — "
                "gain or loss calculated as proceeds minus cost basis (FIFO method). "
                "Receiving BTC as payment is taxable income at NZD value on receipt date. "
                "Internal transfers between your own BTC wallets are not taxable events. "
                "Asset symbol: BTC. Decimal places: 8 (satoshis). Layer 2: Lightning Network."
            ),
            "domain": "cryptocurrency",
            "extra_meta": {
                "asset":           "BTC",
                "chain":           "bitcoin",
                "address_formats": ["legacy:1...", "p2sh:3...", "bech32:bc1..."],
                "address_regex":   r"(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{33}|bc1[a-z0-9]{39,59})",
                "nz_tax_events":   ["disposal", "acquisition", "income"],
                "intent_signals":  ["bitcoin", "BTC", "btc address", "satoshi"],
            },
        },
        {
            "seed_id": "crypto_seed_v1_nz_tax_framework",
            "key":     "semantic:domain:cryptocurrency:nz-tax-framework",
            "title":   "NZ cryptocurrency tax framework — IRD treatment, FY dates, event types",
            "content": (
                "In New Zealand, cryptocurrency is treated as property for tax purposes by the IRD (Inland Revenue Department). "
                "The NZ tax year runs from 1 April to 31 March — e.g. FY2026 = 1 Apr 2025 to 31 Mar 2026. "
                "Taxable crypto events: disposal (sale/swap/exchange for fiat), receiving staking rewards, "
                "receiving crypto as income or payment, mining rewards. "
                "Non-taxable: internal transfers between wallets you own, depositing to an exchange you own, "
                "buying crypto with NZD (not a taxable event itself — sets cost basis). "
                "Gains and losses are calculated in NZD at the spot rate on the date of the event. "
                "Cost basis method: FIFO (first-in first-out) is the accepted method. "
                "Staking rewards: taxable as income at NZD value on date received. "
                "Reporting: income events go to the income schedule; disposal events show gain/loss. "
                "Digiant files under FY ending 31 March each year. "
                "The /do_tax command generates income.csv and expenses.csv for the nominated tax year."
            ),
            "domain": "cryptocurrency",
            "extra_meta": {
                "jurisdiction":   "NZ",
                "authority":      "IRD",
                "tax_year_start": "April 1",
                "tax_year_end":   "March 31",
                "cost_basis":     "FIFO",
                "intent_signals": ["nz tax", "ird", "crypto tax", "tax year", "capital gains", "NZ IRD"],
            },
        },
    ]


def make_skill_semantic_seed(
    skill_name: str,
    specialists: list,
    tier: str,
    description: str = "",
    operations: list | None = None,
) -> dict:
    """Build a single skill seed dict for a newly installed skill.

    Called by the Skill Harness install step immediately after load() succeeds.
    v2: includes description and operations in content so semantic memory
    reflects inputs, processing, and outputs — not just install location.

    _prev_seed_id triggers cleanup of any sparse v1 entry.
    """
    slug = _slug(skill_name)

    parsed = {
        "description": description,
        "specialists":  specialists,
        "tier":         tier,
        "operations":   [
            {"name": op, "inputs": "", "returns": ""}
            for op in (operations or [])
        ],
    }

    return {
        "seed_id":       f"skill_seed_v2_{skill_name}",
        "_prev_seed_id": f"skill_seed_v1_{skill_name}",
        "key":           f"semantic:intent:{slug}",
        "title":         f"Skill: {skill_name}",
        "content":       _build_skill_content(skill_name, parsed),
        "domain":        "skills",
        "extra_meta": {
            "intent_signals": [skill_name, skill_name.replace("-", " ")],
            "action":         f"nanobot:skill:{skill_name}",
            "trigger_point":  f"nanobot:{skill_name}",
            "owner":          "nanobot",
            "operations":     operations or [],
            "specialists":    specialists,
            "tier":           tier,
            "success_count":  0,
            "failure_count":  0,
        },
    }
