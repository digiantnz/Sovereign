import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from governance.engine import GovernanceEngine
from execution.engine import ExecutionEngine
from cognition.engine import CognitionEngine
from execution.adapters.qdrant import QdrantAdapter
from execution.adapters.signing import SigningAdapter
from security.audit_ledger import AuditLedger
from security.soul_guardian import SoulGuardian, load_soul_md
from security.scanner import SecurityScanner
from security.guardrail import GuardrailEngine
from monitoring.metrics import collect_all
from monitoring.scheduler import start_scheduler, start_archive_sync, start_observe_loop
from monitoring.eth_watcher import start_eth_watcher
from skills.loader import scan_all_skills
from skills.lifecycle import load_skill_watchlist
from api.portal import router as portal_router
from execution.adapters.wallet import WalletAdapter
from scheduling.task_scheduler import TaskScheduler
from execution.credential_proxy import CredentialProxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Step 0: Load Sovereign soul (identity doc — FIRST action before anything else)
    try:
        soul_content = load_soul_md()
        logger.info("Sovereign-soul.md loaded (%d chars)", len(soul_content))
    except RuntimeError as e:
        logger.critical("%s", e)
        raise

    # ── Step 1: Security layer init
    ledger = AuditLedger()

    # ── Step 1a: Signing adapter — attach to ledger so all entries get rex_sig
    signer = None
    try:
        signer = SigningAdapter()
        _ = signer.public_key_pem()   # validates key is readable
        ledger.attach_signer(signer)
        logger.info("SigningAdapter: Ed25519 key loaded — all ledger entries will be signed")
    except Exception as e:
        logger.warning("SigningAdapter: key unavailable (%s) — ledger will run unsigned", e)

    # Load durable skill watchlist and merge into soul guardian protected files
    # so dynamically installed skills are checksummed alongside core identity files.
    from security.soul_guardian import PROTECTED_FILES as _BASE_PROTECTED
    _skill_paths = load_skill_watchlist()
    _all_protected = list(_BASE_PROTECTED) + [
        p for p in _skill_paths if p not in _BASE_PROTECTED
    ]
    guardian = SoulGuardian(protected_files=_all_protected)
    drifted = await guardian.verify_and_notify(ledger=ledger)
    if drifted:
        logger.warning("SOUL GUARDIAN: drift detected in %s — see security-ledger.jsonl", drifted)
    else:
        logger.info("SoulGuardian: all protected files verified clean")

    soul_checksum = guardian.get_checksum("/home/sovereign/personas/sovereign-soul.md")
    logger.info("Sovereign-soul.md SHA256: %s", soul_checksum)

    scanner = SecurityScanner()
    scanner.load()
    logger.info("SecurityScanner: patterns loaded")

    # ── API key sanity checks (warn early so failures are diagnosable)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY is not set — Claude API calls will fail with 401. "
                       "Set it in secrets/claude.env and rebuild.")
    if not os.environ.get("GROK_API_KEY"):
        logger.warning("GROK_API_KEY is not set — Grok API calls will fail with 401. "
                       "Set it in secrets/grok.env and rebuild.")

    guardrail = GuardrailEngine(scanner, ledger)

    # ── Step 2: Core services
    qdrant = QdrantAdapter()
    await qdrant.setup()

    # ── Step 2-zero: Sovereign root — cognitive anchor (FIRST semantic write)
    # Writes semantic:entity:sovereign with sov_id=00000000-0000-0000-0000-000000000001.
    # This is the ONLY entry where parent_sov_id=None. Idempotent.
    try:
        await qdrant.seed_sovereign_root()
        logger.info("Sovereign root entry seeded (cognitive anchor)")
    except Exception as _root_err:
        logger.warning("Sovereign root seed failed (non-fatal): %s", _root_err)

    # ── Step 2-one: Entity registry seed
    # Writes semantic:entity:{slug} entries for all 12 foundational entities (002–013).
    # entity_type used (not component_type). Hardware entities (003–006) parent to
    # sovereign root. External entities (persons, orgs, networks, blockchains) have
    # parent_sov_id=None. Also writes semantic:governance:entity-registry index.
    # Must run before component seeds (components reference hardware entity sov_ids).
    try:
        from memory.entity_registry import build_entity_seeds, build_entity_index
        _entity_seeds = build_entity_seeds()
        _entity_index = build_entity_index()
        _entity_audit = await qdrant.seed_entity_entries(_entity_seeds, _entity_index)
        logger.info(
            "Entity registry seed: total=%d created=%d existed=%d errors=%d",
            _entity_audit["total"], _entity_audit["created"],
            _entity_audit["already_existed"], _entity_audit["errors"],
        )
    except Exception as _ent_err:
        logger.warning("Entity registry seed failed (non-fatal): %s", _ent_err)

    # ── Step 2a: Memory Index Protocol — startup migration
    # Stamps _no_key=True on any pre-MIP sovereign entry lacking a _key field.
    # Idempotent — already-tagged entries are skipped. No re-embedding.
    _mip_migrated = await qdrant.startup_migration()
    if _mip_migrated:
        logger.info("MIP startup_migration: patched %d legacy entries with _no_key=True", _mip_migrated)
    else:
        logger.info("MIP startup_migration: all sovereign entries already tagged")

    # ── Step 2b: High-value static fact backfill (idempotent — checks _backfill_seed_id)
    _sovereign_lan_ip = os.environ.get("SOVEREIGN_LAN_IP", "")
    _static_facts = [
        {
            "seed_id": "backfill_v1_wallet_eth_address",
            "key": "semantic:wallet:eth_address",
            "title": "Sovereign ETH wallet owner address (EOA)",
            "content": (
                "Sovereign ETH wallet owner address: 0x50BF8f009ECC10DB65262c65d729152e989A9323. "
                "This is the externally-owned account (EOA) that owns the Safe multisig."
            ),
            "domain": "wallet",
        },
        {
            "seed_id": "backfill_v1_safe_contract",
            "key": "semantic:wallet:safe_contract_address",
            "title": "Safe multisig contract address (2-of-3)",
            "content": (
                "Safe multisig contract address: 0x50BF8f009ECC10DB65262c65d729152e989A9323. "
                "Threshold 2-of-3. Used for all significant on-chain transactions requiring "
                "multi-party approval."
            ),
            "domain": "wallet",
        },
        {
            "seed_id": "backfill_v1_tailscale_ip",
            "key": "semantic:networking:tailscale_ip",
            "title": "Sovereign host Tailscale IP address",
            "content": (
                "Sovereign host Tailscale IP: 100.111.130.60. "
                "This is the Tailscale network address for secure remote access."
            ),
            "domain": "networking",
        },
        {
            "seed_id": "backfill_v1_tailscale_hostname",
            "key": "semantic:networking:tailscale_hostname",
            "title": "Sovereign Tailscale hostname for Nextcloud and admin access",
            "content": (
                "Sovereign Tailscale hostname: sovereign.tail887d2b.ts.net. "
                "This is the external DNS hostname used to access Nextcloud and admin operations."
            ),
            "domain": "networking",
        },
        {
            "seed_id": "backfill_v1_node04_ip",
            # Key changed :networking: → :network:host: to force reseed with corrected content
            # (old entry didn't clarify that Rex accesses via nanobot-01 skills, not directly).
            "key": "semantic:network:host:node04",
            "title": "node04 external services host IP and ports",
            "content": (
                "node04 IP: 172.16.201.4 (VLAN 172.16.201.0/24). "
                "Hosts a2a-browser (web search + URL fetch proxy, port 8001) and "
                "a2a-whisper (speech transcription, port 8003). "
                "Rex uses these services through nanobot-01 skills (sovereign-browser, whisper) "
                "— not by calling node04 directly."
            ),
            "domain": "networking",
        },
        {
            "seed_id": "backfill_v1_nextcloud_url",
            "key": "semantic:networking:nextcloud_url",
            "title": "Nextcloud URL and access endpoints",
            "content": (
                "Nextcloud URL: https://sovereign.tail887d2b.ts.net (Tailscale, port 443/8443). "
                "Internal service name: nextcloud (business_net). "
                "WebDAV and CalDAV are the primary interfaces used by sovereign-core."
            ),
            "domain": "networking",
        },
        {
            "seed_id": "backfill_v1_ollama_endpoint",
            "key": "semantic:infrastructure:ollama_endpoint",
            "title": "Ollama endpoint and active models",
            "content": (
                "Ollama endpoint: http://ollama:11434 (ai_net). "
                "Primary model: llama3.1:8b-instruct-q4_K_M on RTX 3060 Ti (8 GB VRAM). "
                "Also has mistral:7b installed. Embedding model: nomic-embed-text (768-dim)."
            ),
            "domain": "infrastructure",
        },
        {
            "seed_id": "backfill_v1_qdrant_endpoint",
            "key": "semantic:infrastructure:qdrant_endpoint",
            "title": "Qdrant memory: tmpfs working_memory + RAID sovereign collections",
            "content": (
                "Qdrant working_memory: http://qdrant:6333 (tmpfs, on_disk=False, ephemeral). "
                "Qdrant sovereign archive: http://qdrant-archive:6333 (RAID, /home/sovereign/vector, durable). "
                "7 RAID collections: semantic, episodic, prospective, procedural, associative, relational, meta. "
                "working_memory pre-warmed at startup from RAID (top-50/collection, 2GB limit). "
                "Promotions: Rex instruction, PASS 4 decision, or clean shutdown → working_memory → RAID. "
                "Crash without clean shutdown: un-promoted working_memory entries are lost (known risk). "
                "Embeddings: nomic-embed-text (768-dim) via CPU-only ollama-embed (http://ollama-embed:11434). "
                "Inference/key generation: llama3.1:8b via GPU ollama (http://ollama:11434)."
            ),
            "domain": "infrastructure",
        },
    ]
    if _sovereign_lan_ip:
        _static_facts.append({
            "seed_id": "backfill_v1_sovereign_lan_ip",
            "key": "semantic:networking:sovereign_lan_ip",
            "title": "Sovereign host LAN IP address",
            "content": (
                f"Sovereign host LAN IP: {_sovereign_lan_ip}. "
                "Primary internal network address of the machine running Docker Compose."
            ),
            "domain": "networking",
        })
    # Governance — soul checksum (dynamic: updated each boot if changed)
    if soul_checksum:
        _static_facts.append({
            "seed_id": "backfill_v1_soul_checksum",
            "key": "semantic:governance:soul_checksum",
            "title": "Sovereign soul document SHA256 checksum",
            "content": (
                f"Sovereign-soul.md SHA256 checksum: {soul_checksum}. "
                "This is the integrity fingerprint of the sovereign identity document. "
                "The soul-guardian verifies this on every boot. "
                "If the checksum changes, a governance alert is raised."
            ),
            "domain": "governance",
        })
    # Network endpoints — RPC nodes and infrastructure (MIP-as-config pattern)
    # Non-secret infrastructure endpoints live here, not in .env files.
    # sov-wallet watcher reads these at startup to avoid hardcoded IPs.
    _net_endpoints = [
        {
            "seed_id": "backfill_v1_net_eth_primary",
            "key": "semantic:network:endpoints:eth-node-primary",
            "title": "ETH node RPC primary endpoint",
            "content": (
                "Ethereum RPC primary endpoint: 172.16.201.15:8545. "
                "Local Ethereum execution node on the Sovereign VLAN. "
                "Protocol: HTTP JSON-RPC 2.0. Chain: ETH mainnet (chain ID 1). "
                "Also hosts the Beacon API on port 5052. Status: active."
            ),
            "domain": "network.endpoints",
            "label": "eth-node-primary",
            "value": "172.16.201.15:8545",
            "metadata": {"protocol": "http", "chain": "eth", "status": "active", "beacon_port": 5052},
        },
        {
            "seed_id": "backfill_v1_net_eth_secondary",
            "key": "semantic:network:endpoints:eth-node-secondary",
            "title": "ETH node RPC secondary endpoint",
            "content": (
                "Ethereum RPC secondary endpoint: 172.16.201.2:8545. "
                "Fallback Ethereum execution node on the Sovereign VLAN. "
                "Protocol: HTTP JSON-RPC 2.0. Chain: ETH mainnet. Status: active."
            ),
            "domain": "network.endpoints",
            "label": "eth-node-secondary",
            "value": "172.16.201.2:8545",
            "metadata": {"protocol": "http", "chain": "eth", "status": "active"},
        },
        {
            "seed_id": "backfill_v1_net_btc_rpc",
            "key": "semantic:network:endpoints:btc-node-rpc",
            "title": "Bitcoin node RPC endpoint (Start9)",
            "content": (
                "Bitcoin RPC endpoint: 172.16.201.5:8332. "
                "Start9 self-hosted Bitcoin full node on the Sovereign VLAN. "
                "Also hosts Specter Desktop on port 25441 for multisig coordination. "
                "Protocol: HTTP JSON-RPC 1.0 with basic auth (credentials in secrets/wallet.env). "
                "Chain: BTC mainnet. Status: active."
            ),
            "domain": "network.endpoints",
            "label": "btc-node-rpc",
            "value": "172.16.201.5:8332",
            "metadata": {"protocol": "http", "chain": "btc", "status": "active", "host": "start9"},
        },
        {
            "seed_id": "backfill_v1_net_specter",
            "key": "semantic:network:endpoints:specter",
            "title": "Specter Desktop endpoint (Start9)",
            "content": (
                "Specter Desktop endpoint: 172.16.201.5:25441. "
                "Multisig wallet coordination UI on the Start9 node. "
                "Used for BTC multisig signing and PSBT coordination. "
                "Status: active. Wallet: 'Sovereign MultiSig' (2-of-3 P2WSH)."
            ),
            "domain": "network.endpoints",
            "label": "specter",
            "value": "172.16.201.5:25441",
            "metadata": {"protocol": "http", "chain": "btc", "status": "active", "host": "start9"},
        },
        {
            "seed_id": "backfill_v1_net_btcpay",
            "key": "semantic:network:endpoints:btcpay",
            "title": "BTCPay Server endpoint (Start9 — pending configuration)",
            "content": (
                "BTCPay Server endpoint: 172.16.201.5 (port TBD). "
                "Lightning Network payment processor on the Start9 node. "
                "Currently being configured — port and credentials not yet assigned. "
                "Will be used for Lightning/BTCPay wallet.lightning module. Status: pending."
            ),
            "domain": "network.endpoints",
            "label": "btcpay",
            "value": "172.16.201.5",
            "metadata": {"protocol": "http", "chain": "btc", "status": "pending", "host": "start9", "port": None},
        },
        {
            "seed_id": "static_v1_claude_api",
            "key": "semantic:network:service:claude-api",
            "title": "Anthropic Claude API (external LLM provider)",
            "content": (
                "Claude API endpoint: https://api.anthropic.com/v1/messages. "
                "Model: claude-sonnet-4-6 (DEFAULT_MODEL in adapters/claude.py). "
                "Auth: ANTHROPIC_API_KEY env var from secrets/claude.env. "
                "Called via CognitionEngine.ask_claude() — DCL-gated, audit-logged. "
                "Used for high-complexity PASS 2 routing (architectural/plan/review/design/strategy signals). "
                "Reachability: probed by collect_external_reachability() in monitoring/metrics.py as 'claude_api'. "
                "If blank API key: all calls return HTTP 401 — sovereign-core falls back to Ollama silently. "
                "To check status: ask Rex for system metrics or check the portal dashboard external section."
            ),
            "domain": "network.service",
            "label": "claude-api",
            "value": "https://api.anthropic.com/v1/messages",
            "metadata": {"provider": "anthropic", "model": "claude-sonnet-4-6", "auth": "ANTHROPIC_API_KEY", "status": "active"},
        },
        {
            "seed_id": "static_v1_grok_api",
            "key": "semantic:network:service:grok-api",
            "title": "xAI Grok API (external LLM provider)",
            "content": (
                "Grok API endpoint: https://api.x.ai/v1. "
                "Model: grok-3 (default). "
                "Auth: GROK_API_KEY env var from secrets/grok.env. "
                "Called via CognitionEngine.ask_grok() — DCL-gated, audit-logged. "
                "Used for current-events/news/market PASS 2 routing (current/latest/news/today/recent/market signals). "
                "Reachability: probed by collect_external_reachability() in monitoring/metrics.py as 'grok_api'. "
                "To check status: ask Rex for system metrics or check the portal dashboard external section."
            ),
            "domain": "network.service",
            "label": "grok-api",
            "value": "https://api.x.ai/v1",
            "metadata": {"provider": "xai", "model": "grok-3", "auth": "GROK_API_KEY", "status": "active"},
        },
        {
            "seed_id": "backfill_v1_net_a2a_browser",
            # Key changed from :endpoints: → :service: to force delete-and-reseed of the
            # stale v1 entry that contained "POST /run" — Ollama was regurgitating that
            # endpoint as a URL to the Director.
            "key": "semantic:network:service:a2a-browser",
            "title": "a2a-browser web proxy service (node04)",
            "content": (
                "a2a-browser is the external web proxy service running on node04 (172.16.201.4:8001). "
                "It provides SearXNG web search and URL fetch with per-host credential profiles, "
                "and receives A2A payment rail notifications (wallet/credit). "
                "Rex accesses this service exclusively through the sovereign-browser skill on "
                "nanobot-01 — sovereign-core never calls a2a-browser directly. "
                "Auth: X-API-Key shared secret. Status: active."
            ),
            "domain": "network.service",
            "label": "a2a-browser",
            "value": "172.16.201.4:8001",
            "metadata": {"protocol": "http", "status": "active", "host": "node04"},
        },
    ]
    _static_facts.extend(_net_endpoints)

    # Wallet — BTC xpub placeholder (wallet pending first-boot key derivation)
    _static_facts.append({
        "seed_id": "backfill_v1_btc_xpub",
        "key": "semantic:wallet:btc_xpub",
        "title": "Bitcoin BIP-32 xpub (pending wallet first boot)",
        "content": (
            "Bitcoin BIP-32 xpub key: not yet derived. "
            "The sov-wallet container derives the xpub from the BIP-39 seed on first boot. "
            "Run `docker compose up sov-wallet` to initialise. "
            "The xpub will be available via `get_btc_xpub` intent after initialisation."
        ),
        "domain": "wallet",
    })
    # Wallet price feed — CoinGecko for USD normalisation of payment events
    _static_facts.append({
        "seed_id": "backfill_v1_wallet_pricefeed_coingecko",
        "key": "semantic:wallet:pricefeed:coingecko",
        "title": "CoinGecko price feed endpoint for USD normalisation",
        "content": (
            "CoinGecko price feed endpoint: https://api.coingecko.com/api/v3/simple/price. "
            "Used for USD normalisation of wallet payment events before a2a-browser credit dispatch. "
            "Query: ids=ethereum|bitcoin, vs_currencies=usd. "
            "Free public API, no key required. Failure blocks credit dispatch — never estimate. "
            "Status: active."
        ),
        "domain": "wallet.pricefeed",
        "label": "coingecko",
        "value": "https://api.coingecko.com/api/v3/simple/price",
        "metadata": {"provider": "coingecko", "status": "active", "auth": "none"},
    })
    # Director news preferences — used by news_harness to weight synthesis
    _static_facts.append({
        "seed_id": "static_v1_news_preferences",
        "key": "semantic:preferences:news",
        "title": "Director news preferences",
        "content": (
            "Matt's news interests: technology and open source (Hacker News), "
            "artificial intelligence and LLMs, cryptocurrency particularly Ethereum and "
            "Rocket Pool staking, New Zealand local news and current events, "
            "cybersecurity and infosec. "
            "Prefer: substantive technical items over hype; NZ relevance where available. "
            "Avoid: celebrity/entertainment, sports (unless NZ), pure marketing."
        ),
        "domain": "preferences.news",
        "metadata": {"category": "preferences", "subject": "news"},
    })

    _backfilled = await qdrant.seed_static_facts(_static_facts)
    if _backfilled:
        logger.info("MIP static backfill: %d high-value facts seeded with proper keys", _backfilled)

    # ── Step 2c: Tag existing high-value semantic entries with canonical MIP keys
    # Uses set_payload() — no re-embedding. Idempotent (already-keyed entries skipped).
    _tag_patterns = [
        {
            "match": "Governance tiers enforce confirmation gates:",
            "key": "semantic:governance:confirmation_tiers",
            "title": "Sovereign governance tier confirmation gate definitions (LOW/MID/HIGH)",
        },
        {
            "match": "Implementation of Sovereign Secure Signing",
            "key": "semantic:governance:ed25519_signing",
            "title": "Ed25519 keypair and signing adapter implementation",
        },
        {
            "match": "Sovereign-soul.md (identity document) is distinct from CEO_SOUL.md",
            "key": "semantic:governance:soul_document",
            "title": "Sovereign-soul.md identity document vs CEO_SOUL.md persona document",
        },
    ]
    _tagged = await qdrant.tag_high_value_entries(_tag_patterns)
    if _tagged:
        logger.info("MIP stage-4 tagging: %d existing entries assigned canonical keys", _tagged)

    # Seed durable procedural entries (idempotent — no-op if already present)
    _skill_seeded = await qdrant.seed_skill_install_procedure()
    if _skill_seeded:
        logger.info("Qdrant: skill install sequence procedural entry seeded")

    # ── Step 2d: Intent / skill / harness semantic entry seed (Beta-1)
    # Writes a semantic:intent:{slug} entry for every INTENT_ACTION_MAP entry,
    # every installed RAID skill, and the 4 harnesses. Idempotent via _backfill_seed_id.
    # v2 skill seeds: include description + operations I/O; _prev_seed_id triggers v1 cleanup.
    # Tax address placeholders: semantic:tax:taxable_wallets / staking_contracts
    try:
        from memory.semantic_seeds import (
            build_intent_seeds, build_skill_seeds, build_harness_seeds,
            build_tax_address_seeds, build_crypto_domain_seeds,
        )
        from execution.engine import INTENT_ACTION_MAP as _IAM
        _sem_seeds = (
            build_intent_seeds(_IAM)
            + build_skill_seeds()
            + build_harness_seeds()
            + build_tax_address_seeds()
            + build_crypto_domain_seeds()
        )
        _sem_audit = await qdrant.seed_intent_semantic_entries(_sem_seeds)
        logger.info(
            "Intent semantic seed: total=%d created=%d existed=%d errors=%d",
            _sem_audit["total"], _sem_audit["created"],
            _sem_audit["already_existed"], _sem_audit["errors"],
        )
    except Exception as _sem_err:
        logger.warning("Intent semantic seed failed (non-fatal): %s", _sem_err)

    # ── Step 2e: Component registry seed (Beta-2)
    # Writes semantic:component:{name} entries for all ~69 system components +
    # meta:system:component-registry aggregate index. Idempotent via _backfill_seed_id.
    try:
        from memory.component_registry import build_component_seeds, build_component_index
        _comp_seeds = build_component_seeds()
        _comp_index = build_component_index(_comp_seeds)
        _comp_audit = await qdrant.seed_component_entries(_comp_seeds, _comp_index)
        logger.info(
            "Component registry seed: total=%d created=%d existed=%d errors=%d",
            _comp_audit["total"], _comp_audit["created"],
            _comp_audit["already_existed"], _comp_audit["errors"],
        )
    except Exception as _comp_err:
        logger.warning("Component registry seed failed (non-fatal): %s", _comp_err)

    # ── Step 2f: System record seeds (Beta-2)
    # Writes governance invariants, adapter rules, nanobot invariants, domain rules,
    # and as-built architecture decisions to SEMANTIC collection. Historical entries
    # carry status="historical" and are excluded from routing searches automatically.
    try:
        from memory.system_record_seeds import (
            build_governance_seeds,
            build_adapter_invariant_seeds,
            build_nanobot_invariant_seeds,
            build_governance_domain_rule_seeds,
            build_as_built_seeds,
        )
        _sys_seeds = (
            build_governance_seeds()
            + build_adapter_invariant_seeds()
            + build_nanobot_invariant_seeds()
            + build_governance_domain_rule_seeds("/app/governance/governance.json")
            + build_as_built_seeds()
        )
        _sys_audit = await qdrant.seed_intent_semantic_entries(_sys_seeds)
        logger.info(
            "System record seed: total=%d created=%d existed=%d errors=%d",
            _sys_audit["total"], _sys_audit["created"],
            _sys_audit["already_existed"], _sys_audit["errors"],
        )
    except Exception as _sys_err:
        logger.warning("System record seed failed (non-fatal): %s", _sys_err)

    # ── Step 2g: Pre-warm working_memory from RAID sovereign collections.
    # Runs AFTER all seeds so startup_load() reads the corrected RAID state —
    # any stale entries deleted/reseeded by steps 2b–2f are not loaded.
    # Seeds write to RAID (archive_client) only; no working_memory dependency.
    await qdrant.startup_load()

    # ── Step 2h: Bootstrap working_memory from sovereign root seed keys
    # Reads working_memory_seed_keys from semantic:entity:sovereign entry and
    # pre-loads each key into working_memory with stored vectors from RAID.
    # Runs AFTER startup_load() so targeted keys overlay the broad preload.
    try:
        _boot_count = await qdrant.bootstrap_working_memory()
        logger.info("Bootstrap working_memory: %d seed keys loaded", _boot_count)
    except Exception as _boot_err:
        logger.warning("Bootstrap working_memory failed (non-fatal): %s", _boot_err)

    app.state.gov      = GovernanceEngine("/app/governance/governance.json")
    app.state.cog      = CognitionEngine(qdrant, ledger=ledger)
    # Inject cog into qdrant AFTER all startup seeds complete — ensures boot-time
    # semantic writes (static facts, intent seeds, component registry) do not each
    # trigger a structural synthesis task. From this point on, every semantic write
    # fires synthesise_structural(key=<new_key>) as a background asyncio task.
    qdrant.set_cog(app.state.cog)
    app.state.exec     = ExecutionEngine(
        app.state.gov, app.state.cog, qdrant,
        scanner=scanner, guardrail=guardrail, ledger=ledger,
    )
    app.state.guardian    = guardian
    app.state.ledger      = ledger
    app.state.signer      = signer
    app.state.soul_checksum = soul_checksum
    # Inject guardian into execution engine's lifecycle manager
    app.state.exec.set_guardian(guardian)
    # Inject app.state so collect_all() can surface soul_checksum in metrics
    app.state.exec.app_state = app.state

    # ── Step 2b: Sign governance snapshot on startup
    if signer:
        try:
            import hashlib, json as _json
            gov_path = "/app/governance/governance.json"
            with open(gov_path) as f:
                gov_content = f.read()
            gov_hash = hashlib.sha256(gov_content.encode()).hexdigest()
            gov_sig  = signer.sign(gov_hash)
            ledger.append("governance_snapshot", "startup", {
                "file": gov_path,
                "sha256": gov_hash,
                "rex_sig": gov_sig,
            })
            logger.info("Governance snapshot signed: %s", gov_hash[:16])
        except Exception as e:
            logger.warning("Governance snapshot signing failed: %s", e)

        # wallet-config.json — signed alongside governance.json; drift detected on next boot
        try:
            import hashlib as _hl
            wc_path = "/home/sovereign/governance/wallet-config.json"
            with open(wc_path) as f:
                wc_content = f.read()
            wc_hash = _hl.sha256(wc_content.encode()).hexdigest()
            wc_sig  = signer.sign(wc_hash)
            ledger.append("wallet_config_snapshot", "startup", {
                "file": wc_path,
                "sha256": wc_hash,
                "rex_sig": wc_sig,
            })
            logger.info("Wallet config snapshot signed: %s", wc_hash[:16])
        except FileNotFoundError:
            logger.info("wallet-config.json not present yet — skipping snapshot")
        except Exception as e:
            logger.warning("Wallet config snapshot signing failed: %s", e)

    # ── Step 2c: Skill system startup scan
    skill_summary = scan_all_skills(ledger=ledger)
    app.state.skill_summary = skill_summary

    # ── Step 4: Wallet initialization (first-run only)
    wallet = WalletAdapter(signer=signer, ledger=ledger)
    if not wallet.is_initialized():
        logger.info("WalletAdapter: first run detected — initializing sovereign wallet")
        try:
            result = await wallet.initialize()
            logger.info("WalletAdapter: wallet initialized — address: %s", result.get("address"))
        except Exception as e:
            logger.error("WalletAdapter: initialization failed: %s", e)
    else:
        logger.info("WalletAdapter: wallet already initialized — address: %s", wallet.get_address())
    app.state.wallet = wallet

    # ── Step 3: Start self-check scheduler + ETH watcher + hourly RAID archive sync
    _scheduler_task      = start_scheduler(app.state)
    _eth_watcher_task    = start_eth_watcher(ledger=ledger)
    _archive_sync_task   = start_archive_sync(qdrant, ledger)
    # Self-improvement harness — daily observe loop (baseline + anomaly detection)
    # Inject qdrant onto app.state so observe_loop() can access it
    app.state.qdrant = qdrant
    _si_observe_task = start_observe_loop(app.state)

    # ── Step 3b: Task scheduler — data-driven recurring tasks ────────────
    task_scheduler = TaskScheduler(qdrant=qdrant, cog=app.state.cog)
    task_scheduler.set_dispatch_fn(app.state.exec._dispatch)
    app.state.exec.set_task_scheduler(task_scheduler)
    app.state.task_scheduler = task_scheduler
    _task_scheduler_task = task_scheduler.start()
    # Seed nightly dev-harness task (idempotent — no-op if already registered).
    # Runs at 14:00 UTC (02:00 NZST) with trigger="nightly" explicit in step params.
    await task_scheduler.seed_nightly_dev_task()
    await task_scheduler.seed_nightly_synthesis_task()
    await task_scheduler.seed_tax_ingest_task()

    # ── Step 3c: Credential proxy — session-scoped token delegation for nanobot-01
    credential_proxy = CredentialProxy(default_ttl=60, ledger=ledger)
    app.state.credential_proxy = credential_proxy
    app.state.exec.set_credential_proxy(credential_proxy)
    logger.info("CredentialProxy: session-scoped token delegation active")

    # ── Step 3d: Nanobot capability discovery ─────────────────────────────────
    # Fetch agent_card from nanobot-01 /capabilities on startup.
    # Non-fatal — degraded gracefully if nanobot is not yet reachable.
    # Cache is also refreshed on every successful _forward() response.
    try:
        from adapters.nanobot import NanobotAdapter as _NanobotAdapter
        _nb = _NanobotAdapter(ledger=ledger)
        _card = await _nb.fetch_capabilities("nanobot-01")
        if _card:
            logger.info(
                "nanobot-01 capabilities: skills=%s caps=%s",
                _card.get("skills", []), _card.get("capabilities", []),
            )
        else:
            logger.warning("nanobot-01 /capabilities unreachable at startup — will retry on first use")
    except Exception as _e:
        logger.warning("nanobot capability fetch failed: %s", _e)

    yield

    _scheduler_task.cancel()
    _eth_watcher_task.cancel()
    _task_scheduler_task.cancel()
    _archive_sync_task.cancel()
    _si_observe_task.cancel()
    # Promote eligible working_memory entries to RAID sovereign collections before shutdown.
    # Entries not promoted here are lost on container exit (known acceptable risk —
    # see docs/as-built.md; mitigated by 64GB RAM upgrade enabling periodic background flush).
    promoted = await qdrant.shutdown_promote()
    if promoted:
        logger.info("Qdrant shutdown_promote: %d working_memory entries promoted to RAID", promoted)
    else:
        logger.info("Qdrant shutdown_promote: no new entries to promote")


app = FastAPI(lifespan=lifespan)
app.include_router(portal_router)


@app.get("/health")
def health():
    guardian = getattr(app.state, "guardian", None)
    soul_checksum = getattr(app.state, "soul_checksum", None)
    return {
        "status": "ok",
        "soul_checksum": soul_checksum,
        "soul_guardian": "active",
    }


@app.get("/metrics")
async def metrics():
    return await collect_all(app.state)


@app.post("/query")
async def query(payload: dict):
    return await app.state.exec.handle_request(payload)


@app.get("/wallet/verify")
async def wallet_verify(prefix: str):
    wallet = getattr(app.state, "wallet", None)
    if not wallet:
        return {"verified": False, "error": "Wallet adapter not initialized"}
    return wallet.verify_sig(prefix)


@app.post("/wallet_event")
async def wallet_event(request: Request):
    """Receive structured payment events from sov-wallet (A2A JSON-RPC 3.0).

    Called by wallet-harness-a2a.js on confirmed inbound transaction.
    Signs the event with Rex's Ed25519 key, stores in episodic memory,
    formats a plain English Telegram alert via Ollama, sends notification.

    Auth: X-Wallet-Token header matching WALLET_INTERNAL_TOKEN env var.
    Only reachable from ai_net (sov-wallet) — not exposed externally.
    """
    import datetime as _dt
    from execution.adapters.signing import SigningAdapter as _SA

    _wallet_token = os.environ.get("WALLET_INTERNAL_TOKEN", "")
    if not _wallet_token:
        return {"status": "error", "error": "WALLET_INTERNAL_TOKEN not configured"}
    if request.headers.get("X-Wallet-Token", "") != _wallet_token:
        return {"status": "error", "error": "Unauthorized"}, 401

    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "error": "invalid JSON"}

    # Support both raw event dict and A2A JSON-RPC 3.0 wrapper
    if body.get("jsonrpc") == "3.0":
        event = body.get("params", {}).get("payload", {})
        request_id = body.get("id", "")
    else:
        event = body
        request_id = body.get("tx_hash", "")

    required = ("chain", "tx_hash", "amount", "currency", "confirmations", "timestamp")
    missing = [f for f in required if not event.get(f) and event.get(f) != 0]
    if missing:
        return {"status": "error", "error": f"missing fields: {missing}"}

    # Sign the event with Rex's Ed25519 key
    sig = sig_prefix = ""
    try:
        _signer = _SA()
        _canon  = {k: event[k] for k in sorted(event)}
        sig        = _signer.sign_dict(_canon)
        sig_prefix = sig[:8]
    except Exception as e:
        logger.warning("wallet_event: signing failed: %s", e)

    # Write to audit ledger
    ledger = getattr(app.state, "ledger", None)
    if ledger:
        try:
            ledger.append("wallet_payment_event", "wallet_watcher", {
                **event,
                "sig":        sig,
                "sig_prefix": sig_prefix,
                "request_id": request_id,
            })
        except Exception as e:
            logger.warning("wallet_event: ledger write failed: %s", e)

    # Store in episodic memory (Qdrant)
    qdrant = getattr(app.state, "qdrant", None)
    if qdrant:
        try:
            await qdrant.store(
                collection="episodic",
                content=(
                    f"Wallet payment event: {event.get('amount')} {event.get('currency')} "
                    f"on {event.get('chain', '').upper()} — {event.get('label', event.get('to_address', '')[:12])}. "
                    f"Tx: {event.get('tx_hash', '')[:16]}. Confirmations: {event.get('confirmations')}."
                ),
                metadata={
                    "domain":      "wallet.events",
                    "chain":       event.get("chain"),
                    "tx_hash":     event.get("tx_hash"),
                    "amount":      event.get("amount"),
                    "currency":    event.get("currency"),
                    "label":       event.get("label", ""),
                    "sig_prefix":  sig_prefix,
                    "event_ts":    event.get("timestamp"),
                },
                mem_type="episodic",
            )
        except Exception as e:
            logger.warning("wallet_event: qdrant store failed: %s", e)

    # Format and send Telegram notification via Ollama
    cog = getattr(app.state, "cog", None)
    if cog:
        try:
            label = event.get("label") or (event.get("to_address") or "")[:12] + "…"
            result_for_translator = {
                "domain":     "wallet_payment",
                "event_type": "payment_confirmed",
                "chain":      event.get("chain", "").upper(),
                "amount":     event.get("amount"),
                "currency":   event.get("currency"),
                "label":      label,
                "from":       event.get("from_address", "")[:12] + "…",
                "confirmations": event.get("confirmations"),
                "tx_short":   event.get("tx_hash", "")[:16] + "…",
                "verify_cmd": f"/verify {sig_prefix}" if sig_prefix else "",
                "summary":    (
                    f"Received {event.get('amount')} {event.get('currency')} "
                    f"on {event.get('chain', '').upper()} ({label})"
                ),
            }
            alert_text = await cog.translator_pass(result_for_translator, user_input="wallet payment alert")
            # Send Telegram notification
            _token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            _chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
            if _token and _chat_id:
                import httpx as _hx
                async with _hx.AsyncClient(timeout=10.0) as _cl:
                    await _cl.post(
                        f"https://api.telegram.org/bot{_token}/sendMessage",
                        json={"chat_id": _chat_id, "text": alert_text},
                    )
        except Exception as e:
            logger.warning("wallet_event: Telegram notification failed: %s", e)

    # ── A2A Credit Dispatch ───────────────────────────────────────────────────
    # Push wallet/credit notification to a2a-browser on confirmed payment.
    # Sequence: dedup → mark seen (IMMEDIATE) → CoinGecko USD → sign → POST → retry
    _a2a_url  = os.environ.get("A2A_BROWSER_URL", "http://172.16.201.4:8001")
    _a2a_key  = os.environ.get("A2A_SHARED_SECRET", "")
    _cr_chain = event.get("chain", "").lower()
    _cr_tx    = event.get("tx_hash", "")

    if qdrant and _a2a_url and _a2a_key and _cr_tx:
        try:
            import uuid as _uuid_mod
            import httpx as _hx_cr
            from qdrant_client.models import (
                PointStruct as _PS, Filter as _FQ, FieldCondition as _FC, MatchValue as _MV,
            )

            # 1. Dedup check — idempotency gate for credit dispatch
            _credit_pid = str(_uuid_mod.uuid5(
                _uuid_mod.NAMESPACE_URL,
                f"wallet.a2a_credit:{_cr_chain}:{_cr_tx}",
            ))
            _seen_pts, _ = await qdrant.archive_client.scroll(
                collection_name="episodic",
                scroll_filter=_FQ(must=[
                    _FC(key="domain", match=_MV(value="wallet.a2a_credit")),
                    _FC(key="tx_hash", match=_MV(value=_cr_tx)),
                ]),
                limit=1, with_payload=False, with_vectors=False,
            )
            if _seen_pts:
                logger.info("wallet_event: a2a_credit already dispatched for tx %s — skip", _cr_tx[:16])
            else:
                # 2. Mark seen IMMEDIATELY — before normalisation, prevents double-credit on crash
                _cr_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                await qdrant.archive_client.upsert(
                    collection_name="episodic",
                    points=[_PS(
                        id=_credit_pid,
                        vector=[0.0] * 768,
                        payload={
                            "type":     "episodic",
                            "domain":   "wallet.a2a_credit",
                            "chain":    _cr_chain,
                            "tx_hash":  _cr_tx,
                            "seen_at":  _cr_ts,
                            "status":   "pending",
                        },
                    )],
                )

                # 3. USD normalisation via CoinGecko — failure BLOCKS dispatch, never estimate
                _cg_coin_ids = {"eth": "ethereum", "arb": "ethereum", "op": "ethereum", "btc": "bitcoin"}
                _cg_id = _cg_coin_ids.get(_cr_chain)
                _amount_usd: str | None = None
                if _cg_id:
                    try:
                        async with _hx_cr.AsyncClient(timeout=10.0) as _cg:
                            _cg_r = await _cg.get(
                                "https://api.coingecko.com/api/v3/simple/price",
                                params={"ids": _cg_id, "vs_currencies": "usd"},
                            )
                            if _cg_r.status_code == 200:
                                _price = _cg_r.json().get(_cg_id, {}).get("usd")
                                if _price:
                                    _amount_usd = f"{float(event.get('amount', '0')) * float(_price):.2f}"
                    except Exception as _cge:
                        logger.warning("wallet_event: CoinGecko failed: %s", _cge)

                _tg_tok  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                _tg_chat = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")

                if _amount_usd is None:
                    # Price feed failure — block and alert Director
                    logger.error(
                        "wallet_event: price feed failure for tx %s — a2a credit BLOCKED",
                        _cr_tx[:16],
                    )
                    await qdrant.archive_client.set_payload(
                        collection_name="episodic",
                        payload={"status": "price_feed_failed"},
                        points=[_credit_pid],
                    )
                    if _tg_tok and _tg_chat:
                        async with _hx_cr.AsyncClient(timeout=10.0) as _cl:
                            await _cl.post(
                                f"https://api.telegram.org/bot{_tg_tok}/sendMessage",
                                json={"chat_id": _tg_chat, "text": (
                                    f"WALLET ALERT: Price feed failure — a2a credit blocked.\n"
                                    f"Tx: {_cr_tx[:16]}…\n"
                                    f"Amount: {event.get('amount')} {event.get('currency')} ({_cr_chain.upper()})\n"
                                    f"Manual credit required."
                                )},
                            )
                else:
                    # 4. Build A2A 3.0 wallet/credit payload (signed above — sig/sig_prefix reused)
                    _credit_request_id = str(_uuid_mod.uuid4())
                    _a2a_body = {
                        "jsonrpc": "3.0",
                        "id":      _credit_request_id,
                        "method":  "wallet/credit",
                        "params":  {
                            "skill":     "wallet",
                            "operation": "credit",
                            "payload": {
                                "tx_hash":       _cr_tx,
                                "chain":         _cr_chain,
                                "amount_native": event.get("amount"),
                                "currency":      event.get("currency"),
                                "amount_usd":    _amount_usd,
                                "label":         event.get("label", ""),
                                "from_address":  event.get("from_address", ""),
                                "to_address":    event.get("to_address", ""),
                                "confirmations": event.get("confirmations"),
                                "timestamp":     event.get("timestamp"),
                                "sig_prefix":    sig_prefix,
                                "rex_sig":       sig,
                            },
                        },
                    }
                    _a2a_hdrs = {"X-API-Key": _a2a_key, "Content-Type": "application/json"}

                    # 5. First POST attempt
                    _credit_ok  = False
                    _credit_err = ""
                    try:
                        async with _hx_cr.AsyncClient(timeout=30.0) as _ac:
                            _ar = await _ac.post(f"{_a2a_url}/run", json=_a2a_body, headers=_a2a_hdrs)
                        if _ar.status_code == 200:
                            if _ar.json().get("duplicate"):
                                logger.info("wallet_event: a2a-browser duplicate tx %s — ok", _cr_tx[:16])
                            _credit_ok = True
                        else:
                            _credit_err = f"HTTP {_ar.status_code}: {_ar.text[:100]}"
                    except Exception as _ace:
                        _credit_err = str(_ace)[:150]

                    if _credit_ok:
                        await qdrant.archive_client.set_payload(
                            collection_name="episodic",
                            payload={"status": "sent", "amount_usd": _amount_usd,
                                     "credit_id": _credit_request_id},
                            points=[_credit_pid],
                        )
                    else:
                        # First attempt failed — background retry after 30 s
                        logger.warning(
                            "wallet_event: a2a credit first attempt failed (%s) — scheduling retry",
                            _credit_err,
                        )

                        async def _retry_credit(
                            _body=_a2a_body, _hdrs=_a2a_hdrs, _pid=_credit_pid,
                            _tx=_cr_tx, _usd=_amount_usd, _rid=_credit_request_id,
                            _url=_a2a_url,
                        ):
                            await asyncio.sleep(30)
                            _r_ok = False
                            _r_err = ""
                            try:
                                import httpx as _hxr
                                async with _hxr.AsyncClient(timeout=30.0) as _rc:
                                    _rr = await _rc.post(f"{_url}/run", json=_body, headers=_hdrs)
                                if _rr.status_code == 200:
                                    if _rr.json().get("duplicate"):
                                        logger.info(
                                            "wallet_event retry: duplicate tx %s — ok", _tx[:16],
                                        )
                                    _r_ok = True
                                else:
                                    _r_err = f"HTTP {_rr.status_code}: {_rr.text[:100]}"
                            except Exception as _re:
                                _r_err = str(_re)[:150]

                            _pl = {"status": "sent" if _r_ok else "failed",
                                   "credit_id": _rid}
                            if _r_ok:
                                _pl["amount_usd"] = _usd
                            else:
                                _pl["error"] = _r_err
                            try:
                                await qdrant.archive_client.set_payload(
                                    collection_name="episodic",
                                    payload=_pl,
                                    points=[_pid],
                                )
                            except Exception as _qe:
                                logger.warning("wallet_event retry: qdrant update failed: %s", _qe)

                            if not _r_ok:
                                logger.error(
                                    "wallet_event: a2a credit FAILED after retry tx %s: %s",
                                    _tx[:16], _r_err,
                                )
                                _t2 = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                                _c2 = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
                                if _t2 and _c2:
                                    try:
                                        import httpx as _hxal
                                        async with _hxal.AsyncClient(timeout=10.0) as _al:
                                            await _al.post(
                                                f"https://api.telegram.org/bot{_t2}/sendMessage",
                                                json={"chat_id": _c2, "text": (
                                                    f"WALLET ALERT: a2a credit failed — manual review required.\n"
                                                    f"Tx: {_tx[:16]}…\n"
                                                    f"Amount: {_usd} USD\n"
                                                    f"Error: {_r_err}\n"
                                                    f"POST {_url}/run wallet/credit manually."
                                                )},
                                            )
                                    except Exception:
                                        pass

                        asyncio.create_task(_retry_credit())

        except Exception as _a2ae:
            logger.error("wallet_event: a2a credit dispatch error: %s", _a2ae)

    # ── Tax harness — classify wallet event in background ────────────────────
    if qdrant:
        async def _run_tax_classify():
            try:
                from tax_harness.wallet_events import handle_wallet_event as _hwev
                await _hwev(event, qdrant)
            except Exception as _txe:
                logger.warning("wallet_event: tax classify failed: %s", _txe)
        asyncio.create_task(_run_tax_classify())

    return {
        "status":     "ok",
        "chain":      event.get("chain"),
        "tx_hash":    event.get("tx_hash"),
        "sig_prefix": sig_prefix,
        "verify_cmd": f"/verify {sig_prefix}" if sig_prefix else "",
    }


@app.post("/credential_proxy")
async def credential_proxy(payload: dict):
    """Single-use credential token redemption for nanobot-01.

    Called by nanobot-01 server.py before executing a python3_exec op.
    Token was issued by NanobotAdapter.run() and passed in request context.
    Token is invalidated immediately on first redeem.
    Only reachable from ai_net (nanobot-01) — not exposed externally.
    """
    token = payload.get("token", "")
    if not token:
        return {"status": "error", "error": "token required"}
    proxy = getattr(app.state, "credential_proxy", None)
    if not proxy:
        return {"status": "error", "error": "credential proxy not initialized"}
    credentials = proxy.redeem(token)
    if credentials is None:
        return {"status": "error", "error": "invalid, expired, or already-used token"}
    return {"status": "ok", "credentials": credentials}


@app.post("/attachment")
async def attachment(payload: dict):
    """Telegram attachment upload — gateway POSTs file bytes here.

    Payload: {filename, content_b64, mime_type, size, source}
    Decodes base64 content and writes to Nextcloud via WebDAV.
    LOW tier — no confirmation required. Audit-logged.
    """
    return await app.state.exec.handle_attachment(
        filename=payload.get("filename", "unknown"),
        content_b64=payload.get("content_b64", ""),
        mime_type=payload.get("mime_type", "application/octet-stream"),
        size=int(payload.get("size", 0)),
        source=payload.get("source", "unknown"),
    )


@app.post("/chat")
async def chat(payload: dict):
    user_input        = payload.get("input", "")
    pending           = payload.get("pending_delegation")
    confirmed         = payload.get("confirmed", False)
    conf_ack          = payload.get("confidence_acknowledged", False)
    security_conf     = payload.get("security_confirmed", False)
    # context_window: list of {user,assistant} dicts (gateway sends list) or single dict (legacy)
    context_window    = payload.get("context_window")
    # _harness_cmd: set by gateway for /slash commands — bypasses NL routing entirely
    harness_cmd       = payload.get("_harness_cmd")
    return await app.state.exec.handle_chat(
        user_input,
        pending_delegation=pending,
        confirmed=confirmed,
        confidence_acknowledged=conf_ack,
        security_confirmed=security_conf,
        context_window=context_window,
        harness_cmd=harness_cmd,
    )
