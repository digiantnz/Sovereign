import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
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
from monitoring.scheduler import start_scheduler
from monitoring.eth_watcher import start_eth_watcher
from skills.loader import scan_all_skills
from skills.lifecycle import load_skill_watchlist
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

    guardrail = GuardrailEngine(scanner, ledger)

    # ── Step 2: Core services
    qdrant = QdrantAdapter()
    await qdrant.setup()
    await qdrant.startup_load()

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
            "key": "semantic:networking:node04_ip",
            "title": "node04 external services host IP and ports",
            "content": (
                "node04 IP: 172.16.201.4 (VLAN 172.16.201.0/24). "
                "Hosts a2a-browser (port 8001) and a2a-whisper (port 8003). "
                "All external web egress from sovereign-core routes through node04."
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
            "title": "Qdrant endpoint and sovereign collections",
            "content": (
                "Qdrant endpoint: http://qdrant:6333 (ai_net). "
                "7 sovereign RAID-backed collections + ephemeral working_memory. "
                "Vector dimensions: 768 (nomic-embed-text). "
                "Collections: semantic, episodic, prospective, procedural, "
                "associative, relational, meta."
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

    app.state.gov      = GovernanceEngine("/app/governance/governance.json")
    app.state.cog      = CognitionEngine(qdrant, ledger=ledger)
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

    # ── Step 3: Start self-check scheduler + ETH watcher
    _scheduler_task  = start_scheduler(app.state)
    _eth_watcher_task = start_eth_watcher(ledger=ledger)

    # ── Step 3b: Task scheduler — data-driven recurring tasks ────────────
    task_scheduler = TaskScheduler(qdrant=qdrant, cog=app.state.cog)
    task_scheduler.set_dispatch_fn(app.state.exec._dispatch)
    app.state.exec.set_task_scheduler(task_scheduler)
    app.state.task_scheduler = task_scheduler
    _task_scheduler_task = task_scheduler.start()

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
    await qdrant.shutdown_promote()


app = FastAPI(lifespan=lifespan)


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


@app.post("/chat")
async def chat(payload: dict):
    user_input        = payload.get("input", "")
    pending           = payload.get("pending_delegation")
    confirmed         = payload.get("confirmed", False)
    conf_ack          = payload.get("confidence_acknowledged", False)
    security_conf     = payload.get("security_confirmed", False)
    # context_window: list of {user,assistant} dicts (gateway sends list) or single dict (legacy)
    context_window    = payload.get("context_window")
    return await app.state.exec.handle_chat(
        user_input,
        pending_delegation=pending,
        confirmed=confirmed,
        confidence_acknowledged=conf_ack,
        security_confirmed=security_conf,
        context_window=context_window,
    )
