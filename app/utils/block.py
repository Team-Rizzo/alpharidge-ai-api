"""One shared Subtensor connection and a cached chain head."""

import asyncio
import hashlib
import logging
import math
import os
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

NETWORK = os.getenv("SUBTENSOR_NETWORK") or os.getenv("BT_NETWORK", "finney")
BLOCK_CACHE_SECONDS = 12

# Local runs only: with SUBTENSOR_NETWORK=mock the head follows the wall clock, matching
# the subnet's mock chain.
MOCK_BLOCK_SECONDS = float(os.getenv("MOCK_BLOCK_SECONDS", "12"))
MOCK_GENESIS_TS = float(os.getenv("MOCK_GENESIS_TS", "0"))


def is_mock() -> bool:
    return NETWORK == "mock"


def mock_block(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    return max(0, int((now - MOCK_GENESIS_TS) / MOCK_BLOCK_SECONDS))

# The websocket client is not thread-safe, so every chain call holds this lock.
chain_lock = threading.RLock()
_subtensor = None
_block: Optional[int] = None
_block_at = 0.0


def get_subtensor():
    global _subtensor
    with chain_lock:
        if _subtensor is None:
            import bittensor as bt

            _subtensor = bt.Subtensor(network=NETWORK)
            logger.info(f"Connected to subtensor (network={NETWORK})")
        return _subtensor


def reset_subtensor() -> None:
    """Drop the connection after an error so the next call reconnects."""
    global _subtensor
    with chain_lock:
        old, _subtensor = _subtensor, None
    if old is not None:
        try:
            old.substrate.close()
        except Exception:
            pass


def get_current_block() -> Optional[int]:
    """Chain head, cached briefly. Falls back to the last value read, else None."""
    global _block, _block_at
    if is_mock():
        return mock_block()
    with chain_lock:
        if _block is not None and time.time() - _block_at < BLOCK_CACHE_SECONDS:
            return _block
        try:
            _block = int(get_subtensor().get_current_block())
            _block_at = time.time()
        except Exception as e:
            logger.warning(f"Could not read the chain head: {e}")
            reset_subtensor()
        return _block


async def current_block() -> Optional[int]:
    return await asyncio.to_thread(get_current_block)


def _mock_tie_block(after_ms: int) -> Optional[Tuple[int, str]]:
    n = max(0, math.ceil((after_ms / 1000 - MOCK_GENESIS_TS) / MOCK_BLOCK_SECONDS))
    if n > mock_block():
        return None
    return n, "0x" + hashlib.sha256(f"mock:{n}".encode()).hexdigest()


def get_tie_block(after_ms: int) -> Optional[Tuple[int, str]]:
    """The first finalised block whose timestamp is at or after `after_ms`, as (number, hash);
    None until there is one."""
    if is_mock():
        return _mock_tie_block(after_ms)
    with chain_lock:
        try:
            sub = get_subtensor().substrate

            def stamp(n: int) -> int:
                return int(sub.query("Timestamp", "Now", block_hash=sub.get_block_hash(n)).value)

            head = int(sub.get_block_number(sub.get_chain_finalised_head()))
            at = stamp(head)
            if at < after_ms:
                return None
            n = max(1, head - (at - after_ms) // 12_000)
            while n < head and stamp(n) < after_ms:
                n += 1
            while n > 1 and stamp(n - 1) >= after_ms:
                n -= 1
            return n, str(sub.get_block_hash(n))
        except Exception as e:
            logger.warning(f"Could not read the tie block: {e}")
            reset_subtensor()
            return None


async def tie_block(after_ms: int) -> Optional[Tuple[int, str]]:
    return await asyncio.to_thread(get_tie_block, after_ms)
