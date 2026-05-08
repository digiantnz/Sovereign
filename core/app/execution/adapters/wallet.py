"""Sovereign Wallet Adapter.

Handles BIP-39 wallet generation, seed encryption, GPG backup, MetaMask import,
signed audit ledger entries, and transaction proposal signing.

No LLM calls — fully deterministic.
"""

import os
import json
import logging
import asyncio
import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class WalletAdapter:
    SEED_PATH = Path("/home/sovereign/keys/wallet-seed.enc")
    GPG_PATH = Path("/home/sovereign/keys/wallet-seed.gpg")
    STATE_PATH = Path("/home/sovereign/keys/wallet-state.json")
    CEREMONY_PATH = Path("/home/sovereign/docs/sovereign-wallet-keygen.md")
    GPG_PUBKEY_PATH = Path("/home/sovereign/keys/director.gpg.pub")

    def __init__(self, signer, ledger):
        self._signer = signer
        self._ledger = ledger

    def is_initialized(self) -> bool:
        return self.SEED_PATH.exists()

    async def initialize(self) -> dict:
        """First-run: generate wallet, encrypt seed, GPG backup, notify."""
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()
        account, mnemonic = Account.create_with_mnemonic()
        address = account.address

        try:
            # 2. Encrypt seed with HKDF+AES-256-GCM via SigningAdapter
            encrypted = self._signer.encrypt_seed(mnemonic)
            self.SEED_PATH.write_bytes(encrypted)
            os.chmod(self.SEED_PATH, 0o600)

            # 3. GPG backup for Director
            await self._gpg_backup(mnemonic)

            # 4. Store wallet state
            state = {
                "address": address,
                "derivation_path": "m/44'/60'/0'/0/0",
                "initialized_at": datetime.datetime.utcnow().isoformat() + "Z",
            }
            self.STATE_PATH.write_text(json.dumps(state, indent=2))
            os.chmod(self.STATE_PATH, 0o600)

            # 5b. Update wallet-config.json with Rex's address (non-fatal)
            _wc_path = Path("/home/sovereign/governance/wallet-config.json")
            if _wc_path.exists():
                try:
                    _cfg = json.loads(_wc_path.read_text())
                    for _owner in _cfg.get("safe", {}).get("owners", []):
                        if _owner.get("type") == "sovereign-core":
                            _owner["address"] = address
                            break
                    _cfg["updated_at"] = state["initialized_at"]
                    _wc_path.write_text(json.dumps(_cfg, indent=2))
                    logger.info("WalletAdapter: wallet-config.json updated with Rex address %s", address)
                except Exception as _e:
                    logger.warning("WalletAdapter: could not update wallet-config.json: %s", _e)

            # 6. Build signed notification payload
            canonical = {
                "address": address,
                "derivation_path": "m/44'/60'/0'/0/0",
                "generated_at": state["initialized_at"],
                "pubkey_fingerprint": self._signer.public_key_pem()[:64].strip(),
            }
            sig = self._signer.sign_dict(canonical)
            sig_prefix = sig[:8]

            # 7. Log to audit ledger
            self._ledger.append("wallet_keygen", "wallet", {
                "address": address,
                "canonical_payload": canonical,
                "sig": sig,
                "sig_prefix": sig_prefix,
            })

            # 8. Telegram notification
            msg = self._format_keygen_message(address, sig, sig_prefix, state["initialized_at"])
            await self._notify_telegram(msg)

            # 9. Write keygen ceremony doc
            self._write_ceremony_doc(address, sig, sig_prefix, state["initialized_at"])

            return {"status": "ok", "address": address}

        finally:
            # Best-effort mnemonic zeroing
            mnemonic = "x" * len(mnemonic)
            del mnemonic

    def get_address(self) -> "str | None":
        if not self.STATE_PATH.exists():
            return None
        try:
            return json.loads(self.STATE_PATH.read_text())["address"]
        except Exception:
            return None

    def build_proposal(
        self,
        proposal_id: str,
        amount_eth: float,
        to_address: str,
        description: str,
        gas_usd: float,
    ) -> "tuple[str, str]":
        """Sign a transaction proposal. Returns (formatted_message, sig_prefix)."""
        canonical = {
            "proposal_id": proposal_id,
            "action": f"Send {amount_eth} ETH to {to_address}",
            "description": description,
            "gas_estimate_usd": gas_usd,
            "proposed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        sig = self._signer.sign_dict(canonical)
        sig_prefix = sig[:8]
        self._ledger.append("wallet_proposal", "wallet", {
            "canonical_payload": canonical,
            "sig": sig,
            "sig_prefix": sig_prefix,
        })
        msg = (
            f"Rex: {proposal_id}\n"
            f"Action: Send {amount_eth} ETH to {to_address}\n"
            f"  ({description})\n"
            f"Gas estimate: ~${gas_usd:.2f}\n"
            f"Signature: rex_sig:{sig_prefix}...\n"
            f"Verify: /verify {sig_prefix}"
        )
        return msg, sig_prefix

    def verify_sig(self, prefix: str) -> dict:
        """Scan audit ledger for wallet event matching sig prefix, verify it."""
        ledger_path = self._ledger.path
        if not os.path.exists(ledger_path):
            return {"verified": False, "error": "Ledger not found"}
        matches = []
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("event_type") in ("wallet_keygen", "wallet_proposal"):
                    if entry.get("sig_prefix", "").startswith(prefix):
                        matches.append(entry)
        if not matches:
            return {"verified": False, "error": f"No wallet event found with sig prefix '{prefix}'"}
        entry = matches[-1]
        canonical = entry.get("canonical_payload", {})
        full_sig = entry.get("sig", "")
        canonical_str = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        ok = self._signer.verify(canonical_str, full_sig)
        result = {
            "verified": ok,
            "event_type": entry.get("event_type"),
            "sig_prefix": entry.get("sig_prefix"),
            "ts": entry.get("ts"),
        }
        if entry.get("event_type") == "wallet_keygen":
            result["address"] = canonical.get("address")
        else:
            result["action"] = canonical.get("action")
            result["description"] = canonical.get("description")
        return result

    async def _gpg_backup(self, mnemonic: str):
        import gnupg
        import tempfile
        with tempfile.TemporaryDirectory(prefix="sovereign-gpg-") as gpg_home:
            gpg = gnupg.GPG(gnupghome=gpg_home)
            with open(self.GPG_PUBKEY_PATH) as f:
                import_result = gpg.import_keys(f.read())
            if not import_result.fingerprints:
                raise RuntimeError("Failed to import Director GPG public key")
            fingerprint = import_result.fingerprints[0]
            result = gpg.encrypt(mnemonic, recipients=[fingerprint], always_trust=True)
            if not result.ok:
                raise RuntimeError(f"GPG encryption failed: {result.stderr}")
            self.GPG_PATH.write_text(str(result))
            os.chmod(self.GPG_PATH, 0o600)

    async def _notify_telegram(self, message: str):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
        if not token or not chat_id:
            logger.warning("WalletAdapter: Telegram credentials not set — skipping notification")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
                )
        except Exception as e:
            logger.warning("WalletAdapter: Telegram notification failed: %s", e)

    def _format_keygen_message(
        self, address: str, sig: str, sig_prefix: str, generated_at: str
    ) -> str:
        pubkey_short = self._signer.public_key_pem().strip().splitlines()[1][:32] + "..."
        return (
            f"Sovereign Wallet Initialized\n\n"
            f"ETH Address: {address}\n"
            f"Path: m/44'/60'/0'/0/0\n"
            f"Generated: {generated_at}\n\n"
            f"BTC Zpub: run /wallet btc_xpub to export for Specter\n\n"
            f"Rex public key: {pubkey_short}\n"
            f"Signature: rex_sig:{sig_prefix}...\n\n"
            f"To verify this message originated from Rex:\n"
            f"Reply /verify {sig_prefix}"
        )

    def _write_ceremony_doc(
        self, address: str, sig: str, sig_prefix: str, generated_at: str
    ):
        pubkey_pem = self._signer.public_key_pem()
        content = f"""# Sovereign Wallet Key Generation Ceremony

Generated: {generated_at}

## Ethereum Address
```
{address}
```
Derivation path: m/44'/60'/0'/0/0 (BIP-44 Ethereum standard)

## Rex Public Key (Ed25519)
```
{pubkey_pem}
```

## Integrity Signature
The following Ed25519 signature covers the canonical JSON of:
`{{address, derivation_path, generated_at, pubkey_fingerprint}}`

Full signature (base64): `{sig}`
Short prefix for /verify: `{sig_prefix}`

## Encrypted Backups
- `/home/sovereign/keys/wallet-seed.enc` — AES-256-GCM encrypted with key derived from Rex's Ed25519 key via HKDF-SHA256
- `/home/sovereign/keys/wallet-seed.gpg` — OpenPGP encrypted to Director's key (matt@digiant.co.nz)

## Recovery
To recover seed phrase if sovereign.key is available:
```python
from execution.adapters.signing import SigningAdapter
from pathlib import Path
s = SigningAdapter()
enc = Path("/home/sovereign/keys/wallet-seed.enc").read_bytes()
print(s.decrypt_seed(enc))
```

To recover using Director GPG key:
```bash
gpg --decrypt /home/sovereign/keys/wallet-seed.gpg
```

## Audit
Key generation event logged to `/home/sovereign/audit/security-ledger.jsonl`
Event type: `wallet_keygen` | Sig prefix: `{sig_prefix}`
"""
        self.CEREMONY_PATH.write_text(content)


class WalletControlAdapter:
    """Direct eth_account signing adapter for Safe multisig proposals.

    Signs EIP-712 SafeTx typed data directly using Rex's derived ETH private key
    (decrypted from wallet-seed.enc via SigningAdapter.decrypt_seed at call time).
    sov-wallet is used only as a thin internet proxy for the Safe Transaction Service API.

    Tier requirements (enforced by GovernanceEngine upstream — not here):
        MID:  get_address, sign_message, get_pending_proposals
        HIGH: propose_safe_transaction (requires double confirmation)

    Private key material is never logged and is zeroed from bytearrays after use.
    Python string internals cannot be zeroed — scope is kept as narrow as possible.
    """

    _SOV_WALLET_API = os.environ.get("SOV_WALLET_URL", "http://sov-wallet:3001")
    _SAFE_ADDRESS   = os.environ.get("SAFE_ADDRESS", "")
    _CHAIN_ID       = int(os.environ.get("CHAIN_ID", "1"))

    def __init__(self, ledger=None):
        self._ledger = ledger
        self._signer = None

    def _get_signer(self):
        if self._signer is None:
            from execution.adapters.signing import SigningAdapter
            self._signer = SigningAdapter()
        return self._signer

    def _load_eth_account(self):
        """Decrypt seed → derive ETH account at m/44'/60'/0'/0/0.

        Returns (eth_account.Account instance, address string).
        Caller must delete references promptly; mnemonic scope is local only.
        Raises RuntimeError if seed not found or decryption fails.
        """
        import gc
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()

        seed_path = Path("/home/sovereign/keys/wallet-seed.enc")
        if not seed_path.exists():
            raise RuntimeError("wallet-seed.enc not found — wallet not initialized")

        signer = self._get_signer()
        enc = seed_path.read_bytes()
        mnemonic = signer.decrypt_seed(enc)
        try:
            acct = Account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")
            return acct, acct.address
        finally:
            # Best-effort: narrow the mnemonic's lifetime
            del mnemonic
            gc.collect()

    # ── get_address ───────────────────────────────────────────────────────

    async def get_address(self) -> dict:
        """Return Rex's Ethereum address from wallet state. MID tier."""
        state_path = Path("/home/sovereign/keys/wallet-state.json")
        if not state_path.exists():
            result = {"status": "error", "error": "Wallet not initialized"}
        else:
            try:
                state = json.loads(state_path.read_text())
                result = {
                    "status": "ok",
                    "address": state.get("address", ""),
                    "derivation_path": state.get("derivation_path", ""),
                    "initialized_at": state.get("initialized_at", ""),
                }
            except Exception as e:
                result = {"status": "error", "error": str(e)}
        self._audit("wallet_get_address", result)
        return result

    # ── sign_message ──────────────────────────────────────────────────────

    async def sign_message(self, message: str) -> dict:
        """Sign an arbitrary message via eth_account personal_sign. MID tier."""
        from eth_account.messages import encode_defunct
        try:
            acct, address = self._load_eth_account()
            signable = encode_defunct(text=message)
            signed = acct.sign_message(signable)
            signature = "0x" + signed.signature.hex()
            result = {
                "status":    "ok",
                "signature": signature,
                "account":   address,
                "message":   message,
                "method":    "personal_sign",
            }
        except Exception as e:
            logger.error("WalletControlAdapter.sign_message: %s", e)
            result = {"status": "error", "error": str(e)}

        self._audit("wallet_sign_message", {**result, "message": message[:80]})
        return result

    # ── propose_safe_transaction ──────────────────────────────────────────

    async def propose_safe_transaction(
        self, to: str, value: int, data: str, purpose: str
    ) -> dict:
        """Build and submit a Safe multisig proposal as owner #1. HIGH tier.

        Signs EIP-712 SafeTx directly via eth_account (no MetaMask/browser needed).
        Submits to Safe Transaction Service via sov-wallet proxy (browser_net).
        Never executes — creates off-chain proposal only.
        """
        if not self._SAFE_ADDRESS:
            return {"status": "error", "error": "SAFE_ADDRESS not configured — set in secrets/wallet.env"}

        from eth_account.messages import encode_structured_data

        eth_sig = None
        account = None
        nonce   = None

        try:
            # 1. Get Safe nonce via sov-wallet proxy (browser_net → Safe API)
            async with httpx.AsyncClient(timeout=20.0) as client:
                nr = await client.get(
                    f"{self._SOV_WALLET_API}/safe/nonce",
                    params={"safe": self._SAFE_ADDRESS},
                )
                nr.raise_for_status()
                nonce = nr.json()["nonce"]

            # 2. Build EIP-712 SafeTx typed data (Safe v1.3.0 schema)
            typed_data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "chainId",          "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "SafeTx": [
                        {"name": "to",             "type": "address"},
                        {"name": "value",          "type": "uint256"},
                        {"name": "data",           "type": "bytes"},
                        {"name": "operation",      "type": "uint8"},
                        {"name": "safeTxGas",      "type": "uint256"},
                        {"name": "baseGas",        "type": "uint256"},
                        {"name": "gasPrice",       "type": "uint256"},
                        {"name": "gasToken",       "type": "address"},
                        {"name": "refundReceiver", "type": "address"},
                        {"name": "nonce",          "type": "uint256"},
                    ],
                },
                "domain": {
                    "chainId":          self._CHAIN_ID,
                    "verifyingContract": self._SAFE_ADDRESS,
                },
                "primaryType": "SafeTx",
                "message": {
                    "to":             to,
                    "value":          value,
                    "data":           data or "0x",
                    "operation":      0,
                    "safeTxGas":      0,
                    "baseGas":        0,
                    "gasPrice":       0,
                    "gasToken":       "0x0000000000000000000000000000000000000000",
                    "refundReceiver": "0x0000000000000000000000000000000000000000",
                    "nonce":          nonce,
                },
            }

            # 3. Sign directly via eth_account EIP-712
            acct, account = self._load_eth_account()
            signable = encode_structured_data(primitive=typed_data)
            signed   = acct.sign_message(signable)
            eth_sig          = "0x" + signed.signature.hex()
            safe_tx_hash     = "0x" + signed.messageHash.hex()

            # 4. Submit to Safe Transaction Service via sov-wallet proxy
            async with httpx.AsyncClient(timeout=20.0) as client:
                submit = await client.post(
                    f"{self._SOV_WALLET_API}/safe/propose",
                    json={
                        "safe":                     self._SAFE_ADDRESS,
                        "to":                       to,
                        "value":                    str(value),
                        "data":                     data or "0x",
                        "operation":                0,
                        "safeTxGas":                "0",
                        "baseGas":                  "0",
                        "gasPrice":                 "0",
                        "gasToken":                 "0x0000000000000000000000000000000000000000",
                        "refundReceiver":            "0x0000000000000000000000000000000000000000",
                        "nonce":                    nonce,
                        "contractTransactionHash":   safe_tx_hash,
                        "sender":                   account,
                        "signature":                eth_sig,
                        "origin":                   f"Sovereign AI — {purpose}",
                    },
                )
                submit.raise_for_status()

            result = {
                "status":    "ok",
                "safe":      self._SAFE_ADDRESS,
                "to":        to,
                "value_wei": value,
                "nonce":     nonce,
                "purpose":   purpose,
                "account":   account,
            }

        except Exception as e:
            logger.error("WalletControlAdapter.propose_safe_transaction: %s", e)
            result = {"status": "error", "error": str(e)}

        # Sign canonical proposal with Rex's Ed25519 key — anti-spoofing
        canonical = {
            "safe":        self._SAFE_ADDRESS,
            "to":          to,
            "value_wei":   value,
            "purpose":     purpose,
            "proposed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        signer = self._get_signer()
        proposal_sig = signer.sign_dict(canonical)
        sig_prefix   = proposal_sig[:8]
        self._audit("wallet_safe_proposal", {
            **result,
            "canonical_payload": canonical,
            "sig":               proposal_sig,
            "sig_prefix":        sig_prefix,
            "eth_sig":           eth_sig or "",   # full sig in ledger only
        })
        if result["status"] == "ok":
            result["rex_sig_prefix"] = sig_prefix
            result["verify_cmd"]     = f"/verify {sig_prefix}"
        return result

    # ── get_btc_xpub ──────────────────────────────────────────────────────

    async def get_btc_xpub(self) -> dict:
        """Derive Rex's BTC Zpub at m/48'/0'/0'/2' for Specter P2WSH multisig.

        One-time ceremony. Zpub is public key material — safe to share with Specter.
        Decrypts wallet-seed.enc, derives BIP-48 xpub, serialises as Zpub
        (P2WSH mainnet SLIP-132 version 0x02AA7ED3), persists to wallet-config.json,
        sends Telegram notification, and logs a signed audit entry.

        LOW tier — Zpub grants no signing authority.
        """
        import gc
        from embit import bip32, bip39
        from embit.networks import NETWORKS

        seed_path = Path("/home/sovereign/keys/wallet-seed.enc")
        if not seed_path.exists():
            return {"status": "error", "error": "wallet-seed.enc not found — run wallet initialize first"}

        try:
            signer = self._get_signer()
            enc = seed_path.read_bytes()
            mnemonic = signer.decrypt_seed(enc)
            try:
                seed = bip39.mnemonic_to_seed(mnemonic)
                root = bip32.HDKey.from_seed(seed)
                # Master fingerprint needed by Specter for key origin descriptor
                fingerprint = root.my_fingerprint.hex()
                child = root.derive("m/48h/0h/0h/2h")
                # Specter wants standard xpub version bytes at the derivation path
                xpub = child.to_public().to_base58(version=NETWORKS["main"]["xpub"])
                derivation = "m/48'/0'/0'/2'"
                # Full Specter key origin descriptor: [fingerprint/path]xpub
                descriptor = f"[{fingerprint}/{derivation[2:]}]{xpub}"
            finally:
                del mnemonic
                gc.collect()

            exported_at = datetime.datetime.utcnow().isoformat() + "Z"

            # Persist to wallet-config.json
            _wc_path = Path("/home/sovereign/governance/wallet-config.json")
            if _wc_path.exists():
                try:
                    _cfg = json.loads(_wc_path.read_text())
                    _cfg.setdefault("btc", {})["rex_xpub"] = xpub
                    _cfg.setdefault("btc", {})["rex_xpub_path"] = derivation
                    _cfg.setdefault("btc", {})["rex_fingerprint"] = fingerprint
                    _cfg.setdefault("btc", {})["rex_descriptor"] = descriptor
                    _cfg["updated_at"] = exported_at
                    _wc_path.write_text(json.dumps(_cfg, indent=2))
                    logger.info("get_btc_xpub: xpub written to wallet-config.json")
                except Exception as _e:
                    logger.warning("get_btc_xpub: could not update wallet-config.json: %s", _e)

            self._audit("wallet_btc_xpub", {
                "xpub": xpub,
                "fingerprint": fingerprint,
                "derivation_path": derivation,
                "descriptor": descriptor,
                "exported_at": exported_at,
            })

            msg = (
                f"Rex BTC Xpub Exported\n\n"
                f"Derivation: {derivation}\n"
                f"Fingerprint: {fingerprint}\n"
                f"Xpub: {xpub}\n\n"
                f"Specter descriptor:\n{descriptor}\n\n"
                f"Add to Specter Desktop as P2WSH multisig signer (2-of-3).\n"
                f"Exported: {exported_at}"
            )
            await self._notify_telegram(msg)

            return {
                "status": "ok",
                "xpub": xpub,
                "fingerprint": fingerprint,
                "derivation_path": derivation,
                "descriptor": descriptor,
                "exported_at": exported_at,
            }

        except Exception as e:
            logger.error("WalletControlAdapter.get_btc_xpub: %s", e)
            return {"status": "error", "error": str(e)}

    # ── get_pending_proposals ─────────────────────────────────────────────

    async def get_pending_proposals(self) -> dict:
        """Return pending unsigned Safe proposals. MID tier."""
        if not self._SAFE_ADDRESS:
            return {"status": "error", "error": "SAFE_ADDRESS not configured — set in secrets/wallet.env"}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    f"{self._SOV_WALLET_API}/safe/pending",
                    params={"safe": self._SAFE_ADDRESS},
                )
                r.raise_for_status()
                data = r.json()
            result = {"status": "ok", "safe": self._SAFE_ADDRESS, **data}
        except Exception as e:
            result = {"status": "error", "error": str(e)}
        self._audit("wallet_get_proposals", result)
        return result

    # ── sign_btc_psbt ─────────────────────────────────────────────────────

    async def sign_btc_psbt(self, psbt_b64: str) -> dict:
        """Sign a PSBT with Rex's BTC key (m/48'/0'/0'/2').

        HIGH tier — Rex is co-signing a financial transaction.
        The PSBT is externally sourced (BTCPay channel open, Specter, etc.).
        Returns the partially-signed PSBT base64 for the Director to complete
        with a second signature (Ledger or Exodus) via Specter.
        """
        import gc
        import base64 as _b64
        from embit import bip32, bip39
        from embit.psbt import PSBT

        seed_path = Path("/home/sovereign/keys/wallet-seed.enc")
        if not seed_path.exists():
            return {"status": "error", "error": "wallet-seed.enc not found — run wallet initialize first"}
        if not psbt_b64 or not psbt_b64.strip():
            return {"status": "error", "error": "psbt_b64 is required"}

        # Parse and validate PSBT before touching the key
        try:
            psbt_bytes = _b64.b64decode(psbt_b64.strip())
            tx = PSBT.parse(psbt_bytes)
        except Exception as e:
            return {"status": "error", "error": f"invalid PSBT: {e}"}

        input_count = len(tx.inputs)

        # Build output summary for audit (amounts in satoshis)
        outputs_summary = []
        try:
            for out in tx.tx.vout:
                outputs_summary.append({
                    "value_sat": int(out.value),
                    "script": out.script_pubkey.data.hex()[:40],
                })
        except Exception:
            pass

        signed_count = 0
        try:
            signer = self._get_signer()
            enc = seed_path.read_bytes()
            mnemonic = signer.decrypt_seed(enc)
            try:
                seed = bip39.mnemonic_to_seed(mnemonic)
                root = bip32.HDKey.from_seed(seed)
                # Pass root (not account key) so embit derives each input's path from fingerprint
                signed_count = tx.sign_with(root)
            finally:
                del mnemonic, seed
                gc.collect()
        except Exception as e:
            logger.error("sign_btc_psbt: signing failed: %s", e)
            return {"status": "error", "error": f"signing failed: {e}"}

        if signed_count == 0:
            return {
                "status": "error",
                "error": (
                    "Rex's key is not a signer for any input in this PSBT. "
                    "Verify the PSBT was built with the Sovereign MultiSig descriptor."
                ),
            }

        signed_psbt = _b64.b64encode(tx.serialize()).decode()
        signed_at = datetime.datetime.utcnow().isoformat() + "Z"

        self._audit("wallet_btc_psbt_sign", {
            "inputs_signed": signed_count,
            "input_count":   input_count,
            "outputs":       outputs_summary,
            "signed_at":     signed_at,
            "psbt_prefix":   psbt_b64[:32],
        })

        msg = (
            f"BTC PSBT Signed by Rex\n\n"
            f"Inputs signed: {signed_count}/{input_count}\n"
            f"Outputs: {len(outputs_summary)}\n"
            f"Signed: {signed_at}\n\n"
            f"Partially-signed PSBT — import into Specter and co-sign with Ledger or Exodus:\n\n"
            f"{signed_psbt}"
        )
        await self._notify_telegram(msg)

        return {
            "status":        "ok",
            "inputs_signed": signed_count,
            "signed_psbt":   signed_psbt,
            "signed_at":     signed_at,
            "note":          "Import into Specter → sign with Ledger or Exodus → broadcast",
        }

    # ── create_btc_psbt ───────────────────────────────────────────────────

    async def create_btc_psbt(
        self,
        recipient: str,
        amount_btc: str,
        fee_rate: int = 5,
        memo: str = "",
    ) -> dict:
        """Create a PSBT via Specter API, sign with Rex's key, return for Director co-sign.

        HIGH tier — Rex is proposing and co-signing a BTC spend.
        Requires SPECTER_PASSWORD env var in secrets/wallet.env.
        Returns the partially-signed PSBT for Director to complete in Specter.
        """
        import gc
        import base64 as _b64
        from embit import bip32, bip39
        from embit.psbt import PSBT

        _btc_rpc_url  = os.environ.get("BTC_RPC_URL", "")
        _btc_rpc_user = os.environ.get("BTC_RPC_USER", "bitcoin")
        _btc_rpc_pass = os.environ.get("BTC_RPC_PASS", "")

        if not _btc_rpc_url:
            return {"status": "error", "error": "BTC_RPC_URL not configured — add to secrets/wallet.env"}
        if not _btc_rpc_pass:
            return {"status": "error", "error": "BTC_RPC_PASS not configured — add to secrets/wallet.env"}
        if not recipient or not amount_btc:
            return {"status": "error", "error": "recipient and amount_btc are required"}

        seed_path = Path("/home/sovereign/keys/wallet-seed.enc")
        if not seed_path.exists():
            return {"status": "error", "error": "wallet-seed.enc not found"}

        # Load wallet alias from config
        _wallet_alias = "sovereign_multisig"
        try:
            import json as _j
            _cfg = _j.loads(Path("/home/sovereign/governance/wallet-config.json").read_text())
            _wallet_alias = _cfg.get("bitcoin", {}).get("specter_wallet_alias", _wallet_alias)
        except Exception:
            pass

        # 1. Create PSBT via Bitcoin Core RPC (walletcreatefundedpsbt)
        # Specter imports wallets into Core as "specter{uid}/{alias}" — discover name via listwallets
        unsigned_psbt_b64 = None
        try:
            async with httpx.AsyncClient(
                timeout=45.0,
                auth=(_btc_rpc_user, _btc_rpc_pass),
                verify=False,
            ) as client:
                # Find the Core wallet name for this alias
                rpc_base = _btc_rpc_url if _btc_rpc_url.endswith("/") else _btc_rpc_url + "/"
                r_list = await client.post(rpc_base, json={"method": "listwallets", "params": [], "id": 1})
                wallets = r_list.json().get("result", [])
                core_wallet = next((w for w in wallets if w.endswith(f"/{_wallet_alias}")), None)
                if not core_wallet:
                    return {"status": "error", "error": f"Wallet '{_wallet_alias}' not found in Bitcoin Core — is it loaded in Specter?"}

                import urllib.parse as _up
                wallet_url = f"{rpc_base}wallet/{_up.quote(core_wallet, safe='')}"
                amount_float = float(amount_btc)
                r = await client.post(wallet_url, json={
                    "method": "walletcreatefundedpsbt",
                    "params": [
                        [],
                        [{recipient: amount_float}],
                        0,
                        {"fee_rate": fee_rate, "replaceable": True},
                    ],
                    "id": 1,
                })
                resp = r.json()
                if resp.get("error"):
                    return {"status": "error", "error": f"Core walletcreatefundedpsbt: {resp['error']['message']}"}
                unsigned_psbt_b64 = resp["result"]["psbt"]
        except Exception as e:
            _emsg = str(e).split("\nFor more information")[0] or repr(e)
            return {"status": "error", "error": f"Bitcoin Core RPC failed ({type(e).__name__}): {_emsg}"}

        if not unsigned_psbt_b64:
            return {"status": "error", "error": "Bitcoin Core returned no PSBT"}

        # 2. Parse PSBT
        try:
            psbt_bytes = _b64.b64decode(unsigned_psbt_b64.strip())
            tx = PSBT.parse(psbt_bytes)
        except Exception as e:
            return {"status": "error", "error": f"PSBT parse failed after Specter creation: {e}"}

        input_count = len(tx.inputs)

        # 3. Sign with Rex's key
        # Bitcoin Core records Rex's xpub with fingerprint c04605b5 (Specter self-fingerprint,
        # computed from the xpub itself since it was imported without a derivation path).
        # sign_with(root) uses the root fingerprint (cf99ab1f) which doesn't match.
        # We must derive to m/48h/0h/0h/2h first so our fingerprint matches the PSBT.
        signed_count = 0
        try:
            signer = self._get_signer()
            enc = seed_path.read_bytes()
            mnemonic = signer.decrypt_seed(enc)
            try:
                seed = bip39.mnemonic_to_seed(mnemonic)
                root = bip32.HDKey.from_seed(seed)
                # Derive to the xpub level Specter imported — PSBT paths are m/0/0 relative to this
                intermediate = root.derive("m/48h/0h/0h/2h")
                signed_count = tx.sign_with(intermediate)
                del intermediate
            finally:
                del mnemonic, seed
                gc.collect()
        except Exception as e:
            logger.error("create_btc_psbt: signing failed: %s", e)
            return {"status": "error", "error": f"signing failed: {e}"}

        if signed_count == 0:
            return {"status": "error", "error": "Rex's key did not match any input — check wallet alias and descriptor"}

        signed_psbt = _b64.b64encode(tx.serialize()).decode()
        created_at = datetime.datetime.utcnow().isoformat() + "Z"

        # Save PSBT to audit dir so it can be retrieved if the Telegram message is lost
        _ts_slug = created_at.replace(":", "-").replace(".", "-")[:19]
        _psbt_path = Path(f"/home/sovereign/audit/btc-psbt-{_ts_slug}.psbt")
        try:
            _psbt_path.write_text(signed_psbt)
        except Exception:
            _psbt_path = None

        self._audit("wallet_btc_psbt_create", {
            "recipient":     recipient,
            "amount_btc":    amount_btc,
            "fee_rate":      fee_rate,
            "inputs_signed": signed_count,
            "input_count":   input_count,
            "memo":          memo,
            "created_at":    created_at,
            "psbt_file":     str(_psbt_path) if _psbt_path else "not saved",
        })

        _file_note = f"\nBackup: {_psbt_path}" if _psbt_path else ""
        msg = (
            f"BTC Transaction Proposed by Rex\n\n"
            f"To: {recipient}\n"
            f"Amount: {amount_btc} BTC\n"
            f"Fee rate: {fee_rate} sat/vB\n"
            f"Rex signed: {signed_count}/{input_count} inputs\n"
            f"Created: {created_at}{_file_note}\n\n"
            f"Next: Specter → Sovereign MultiSig → Send → Load PSBT → paste → sign Ledger → broadcast\n\n"
            f"{signed_psbt}"
        )
        await self._notify_telegram(msg)

        # Return without signed_psbt and note — translator will hallucinate from those.
        # The PSBT was already delivered via _notify_telegram above.
        return {
            "status":        "ok",
            "recipient":     recipient,
            "amount_btc":    amount_btc,
            "fee_rate_sat_vb": fee_rate,
            "rex_signed":    f"{signed_count}/{input_count} inputs",
            "created_at":    created_at,
            "psbt_file":     str(_psbt_path) if _psbt_path else "audit write failed",
            "next_step":     "Specter → Sovereign MultiSig → Send → Load PSBT → sign Ledger → broadcast",
        }

    # ── helpers ───────────────────────────────────────────────────────────

    async def _notify_telegram(self, message: str):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("OPENCLAW_TELEGRAM_ADMIN_CHAT_ID", "")
        if not token or not chat_id:
            logger.warning("WalletControlAdapter: Telegram credentials not set — skipping notification")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
                )
        except Exception as e:
            logger.warning("WalletControlAdapter: Telegram notification failed: %s", e)

    def _audit(self, event: str, data: dict) -> None:
        if self._ledger:
            try:
                self._ledger.append(event, "wallet_control", data)
            except Exception as e:
                logger.warning("WalletControlAdapter audit failed: %s", e)
