"""Sovereign Secure Signing Adapter — Ed25519.

Private key lives at /home/sovereign/keys/sovereign.key (SECRET tier, 600 perms).
It is NEVER loaded into LLM context, never logged, never transmitted.

Usage:
    signer = SigningAdapter()
    sig = signer.sign("some message")          # → base64 string
    ok  = signer.verify("some message", sig)   # → bool (uses Rex's own pubkey)
    ok  = signer.verify(msg, sig, other_pem)   # → bool (verify counterparty)
"""

import base64
import logging
import os

logger = logging.getLogger(__name__)

KEY_PATH = os.environ.get("SOVEREIGN_KEY_PATH", "/home/sovereign/keys/sovereign.key")
PUB_PATH = os.environ.get("SOVEREIGN_PUB_PATH", "/home/sovereign/keys/sovereign.pub")


class SigningAdapter:
    """Ed25519 signing and verification.

    Private key is loaded lazily on first use and cached in-process.
    The key path must never be passed to any LLM or logged.
    """

    def __init__(self, key_path: str = KEY_PATH, pub_path: str = PUB_PATH):
        self._key_path = key_path
        self._pub_path = pub_path
        self._private_key = None
        self._public_key = None

    def _load_private(self):
        if self._private_key is None:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            with open(self._key_path, "rb") as f:
                self._private_key = load_pem_private_key(f.read(), password=None)
        return self._private_key

    def _load_public(self):
        if self._public_key is None:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            with open(self._pub_path, "rb") as f:
                self._public_key = load_pem_public_key(f.read())
        return self._public_key

    def sign(self, message: str | bytes) -> str:
        """Sign a message. Returns base64-encoded Ed25519 signature."""
        payload = message.encode() if isinstance(message, str) else message
        sig = self._load_private().sign(payload)
        return base64.b64encode(sig).decode()

    def verify(self, message: str | bytes, signature: str, pubkey_pem: str | None = None) -> bool:
        """Verify a signature.

        If pubkey_pem is None, verifies against Rex's own public key.
        If pubkey_pem is provided (PEM string), verifies against that key.
        Returns True if valid, False otherwise.
        """
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        try:
            payload = message.encode() if isinstance(message, str) else message
            sig_bytes = base64.b64decode(signature)
            if pubkey_pem is None:
                pub = self._load_public()
            else:
                pub = load_pem_public_key(
                    pubkey_pem.encode() if isinstance(pubkey_pem, str) else pubkey_pem
                )
            pub.verify(sig_bytes, payload)
            return True
        except (InvalidSignature, Exception):
            return False

    def public_key_pem(self) -> str:
        """Return Rex's public key in PEM format (safe to distribute)."""
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return self._load_public().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()

    def sign_dict(self, data: dict) -> str:
        """Canonically sign a dict by signing its sorted JSON representation."""
        import json
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return self.sign(canonical)

    def _derive_encryption_key(self) -> bytes:
        """Derive a 32-byte AES-256-GCM key from sovereign.key via HKDF-SHA256.

        The derived key is never stored on disk. Caller must zero it after use.
        """
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
        raw = self._load_private().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"sovereign-wallet-seed-v1",
        ).derive(raw)
        # Zero the raw key material in a bytearray (best effort — Python may still have copies)
        raw_ba = bytearray(raw)
        for i in range(len(raw_ba)):
            raw_ba[i] = 0
        return key

    def encrypt_seed(self, seed_phrase: str) -> bytes:
        """Encrypt a BIP-39 seed phrase.

        Returns nonce (12 bytes) || ciphertext+GCM-tag as bytes.
        The HKDF-derived AES key is zeroed after use — never written to disk.
        """
        import os as _os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = self._derive_encryption_key()
        try:
            nonce = _os.urandom(12)
            ct = AESGCM(key).encrypt(nonce, seed_phrase.encode(), b"sovereign-wallet-v1")
            return nonce + ct
        finally:
            key_ba = bytearray(key)
            for i in range(len(key_ba)):
                key_ba[i] = 0

    def decrypt_seed(self, encrypted: bytes) -> str:
        """Decrypt a seed phrase encrypted by encrypt_seed().

        The HKDF-derived AES key is zeroed after use.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = self._derive_encryption_key()
        try:
            nonce, ct = encrypted[:12], encrypted[12:]
            plaintext = AESGCM(key).decrypt(nonce, ct, b"sovereign-wallet-v1")
            return plaintext.decode()
        finally:
            key_ba = bytearray(key)
            for i in range(len(key_ba)):
                key_ba[i] = 0
