import hashlib
import json
import os
from datetime import datetime, timezone

LEDGER_PATH = "/home/sovereign/audit/security-ledger.jsonl"


class AuditLedger:
    def __init__(self, path: str = LEDGER_PATH, signer=None):
        self.path = path
        self._signer = signer   # optional SigningAdapter — injected after key is available
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._prev_hash = self._read_last_hash()

    def attach_signer(self, signer) -> None:
        """Attach a SigningAdapter after ledger init (key may not exist at ledger construction time)."""
        self._signer = signer

    def _read_last_hash(self) -> str:
        if not os.path.exists(self.path):
            return "0" * 64
        try:
            last_line = ""
            with open(self.path, "r") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
            if last_line:
                entry = json.loads(last_line)
                return entry.get("record_hash", "0" * 64)
        except Exception:
            pass
        return "0" * 64

    def append(self, event_type: str, stage: str, data: dict) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "stage": stage,
            **data,
            "prev_hash": self._prev_hash,
        }
        record_hash = hashlib.sha256(
            json.dumps(entry, sort_keys=True).encode()
        ).hexdigest()
        entry["record_hash"] = record_hash

        # Rex signs the record_hash — proves ledger entries originate from Sovereign
        if self._signer:
            try:
                entry["rex_sig"] = self._signer.sign(record_hash)
            except Exception:
                entry["rex_sig"] = None   # key unavailable — log without sig, don't crash

        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        self._prev_hash = record_hash
        return record_hash
