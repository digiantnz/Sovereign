import logging
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
