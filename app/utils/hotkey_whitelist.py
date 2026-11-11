"""Miner and validator hotkeys from the metagraph.

Both lists come from one metagraph sync, run off the event loop by a background job.
Request-time checks only read the cached lists.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List

from app import ROOT
from app.utils import block

logger = logging.getLogger(__name__)

NETUID = int(os.getenv("SUBNET_UID", "45"))
STAKE_THRESHOLD = int(os.getenv("STAKE_THRESHOLD", "20000"))
SYNC_INTERVAL_SECONDS = 2 * 60

# Local/testnet only; never enable in production.
ALLOW_MANUAL_HOTKEYS = os.getenv("ALLOW_MANUAL_HOTKEYS", "false").lower() == "true"


def _csv(key: str) -> List[str]:
    return [hk.strip() for hk in os.getenv(key, "").split(",") if hk.strip()]


MANUAL_VALIDATOR_HOTKEYS = _csv("MANUAL_VALIDATOR_HOTKEYS")
MANUAL_MINER_HOTKEYS = _csv("MANUAL_MINER_HOTKEYS")
# Mock chain only: "hotkey:coldkey" pairs.
MOCK_COLDKEYS = dict(p.split(":", 1) for p in _csv("MOCK_COLDKEYS") if ":" in p)

_MINER_HOTKEYS_FILE = ROOT / "miner_hotkeys.json"
_VALIDATOR_HOTKEYS_FILE = ROOT / "validator_hotkeys.json"

_miners: List[str] = []
_validators: List[str] = []
_coldkeys: Dict[str, str] = {}
_synced_at = 0.0


def _write_snapshot(path, ts: float, hotkeys: List[str], **extra) -> None:
    try:
        path.write_text(json.dumps({
            "last_updated": ts,
            "last_updated_iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "total_count": len(hotkeys),
            "network": block.NETWORK,
            "netuid": NETUID,
            **extra,
            "hotkeys": sorted(hotkeys),
        }, indent=2))
    except Exception as e:
        logger.error(f"Failed to write {path.name}: {e}")


def sync() -> bool:
    """Blocking metagraph sync that refreshes both lists. Returns True on success."""
    global _miners, _validators, _coldkeys, _synced_at
    if block.is_mock():
        # No chain: only the manual lists apply.
        if ALLOW_MANUAL_HOTKEYS:
            _coldkeys = dict(MOCK_COLDKEYS)
        _synced_at = time.time()
        return True
    try:
        with block.chain_lock:
            sub = block.get_subtensor()
            mg = sub.metagraph(NETUID)
            mg.sync(subtensor=sub, lite=True)
        permits = [bool(int(p)) for p in mg.validator_permit]
        miners = [hk for uid, hk in enumerate(mg.hotkeys) if not permits[uid]]
        validators = [hk for uid, hk in enumerate(mg.hotkeys)
                      if permits[uid] and mg.S[uid] >= STAKE_THRESHOLD]
        coldkeys = dict(zip(mg.hotkeys, mg.coldkeys))
    except Exception as e:
        logger.error(f"Metagraph sync failed: {e}")
        block.reset_subtensor()
        return False

    now = time.time()
    _miners, _validators, _coldkeys, _synced_at = miners, validators, coldkeys, now
    logger.info(f"Whitelist synced: {len(miners)} miners, {len(validators)} validators "
                f"(stake >= {STAKE_THRESHOLD})")
    _write_snapshot(_MINER_HOTKEYS_FILE, now, miners)
    _write_snapshot(_VALIDATOR_HOTKEYS_FILE, now, validators,
                    stake_threshold=STAKE_THRESHOLD,
                    validators=[{"name": f"Validator {i + 1}", "hotkey": hk}
                                for i, hk in enumerate(validators)])
    return True


async def sync_async() -> bool:
    return await asyncio.to_thread(sync)


async def run_sync() -> None:
    """Refresh the lists on a fixed interval."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        await sync_async()


def get_miner_hotkeys() -> List[str]:
    if ALLOW_MANUAL_HOTKEYS:
        return list(set(_miners) | set(MANUAL_MINER_HOTKEYS))
    return list(_miners)


def get_validator_hotkeys() -> List[str]:
    if ALLOW_MANUAL_HOTKEYS:
        return list(set(_validators) | set(MANUAL_VALIDATOR_HOTKEYS))
    return list(_validators)


def is_miner_hotkey(hotkey: str) -> bool:
    return hotkey in get_miner_hotkeys()


def is_validator_hotkey(hotkey: str) -> bool:
    return hotkey in get_validator_hotkeys()


def coldkey_of(hotkey: str) -> str:
    """The hotkey's coldkey; the hotkey itself when it is not on the metagraph."""
    return _coldkeys.get(hotkey, hotkey)


def siblings(hotkey: str) -> List[str]:
    """Hotkeys that share the hotkey's coldkey, the hotkey included."""
    coldkey = coldkey_of(hotkey)
    return sorted({hotkey, *(hk for hk, ck in _coldkeys.items() if ck == coldkey)})
