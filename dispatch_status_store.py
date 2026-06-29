"""
In-memory + disk store for per-validator adaptive-dispatch status (RFC 2026-06-28).

Display-only and DECOUPLED from consensus — explanatory data for the miner
dashboard, never an input to scoring/attestation/weights. Latest snapshot per
validator hotkey, persisted to disk so it survives an API restart (loaded on
import, rewritten atomically on each update). Kept out of Prisma deliberately:
it's ephemeral status, not historical records, so no migration is needed.

Shared by main.py (POST ingest) and dashboard_routes.py (GET serve) — a standalone
module so both can import it without a circular dependency.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

_PATH = Path(os.getenv(
    "DISPATCH_STATUS_PATH",
    str(Path(__file__).resolve().parent / ".dispatch_status.json"),
))


def _load() -> dict:
    try:
        if _PATH.exists():
            return json.loads(_PATH.read_text())
    except Exception:
        pass
    return {}


# { validator_hotkey: {"validator_hotkey", "updated", "miners": [ {...}, ... ]} }
_status: Dict[str, dict] = _load()


def set_status(validator_hotkey: str, updated: str, miners: List[Dict[str, Any]]) -> None:
    _status[validator_hotkey] = {
        "validator_hotkey": validator_hotkey,
        "updated": updated,
        "miners": miners,
    }
    try:
        tmp = _PATH.with_name(_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_status))
        tmp.replace(_PATH)
    except Exception:
        pass


def get_all() -> Dict[str, dict]:
    """All validators' latest snapshots."""
    return _status


def get_for_miner(hotkey: str) -> List[dict]:
    """This miner's status across every reporting validator (one row per validator)."""
    out = []
    for vh, snap in _status.items():
        for m in snap.get("miners", []):
            if m.get("hotkey") == hotkey:
                out.append({"validator_hotkey": vh, "updated": snap.get("updated"), **m})
    return out
