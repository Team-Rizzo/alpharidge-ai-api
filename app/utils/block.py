"""One shared Subtensor connection and a cached chain head."""

import asyncio
import hashlib
import logging
import math
import os
import threading
import time
from typing import Dict, Optional, Tuple

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


# Tie lookups read block timestamps from the past, which a pruned node no longer holds; they
# may use their own connection (EL_TIE_NETWORK, such as "archive") and lock.
TIE_NETWORK = os.getenv("EL_TIE_NETWORK", "").strip()
TIE_HEAD_CACHE_S = 6
TIE_STAMPS_MAX = 100_000
_tie_lock = threading.RLock() if TIE_NETWORK else chain_lock
_tie_subtensor = None
# Finalised block number -> (timestamp ms, hash).
_stamps: Dict[int, Tuple[int, str]] = {}
_tie_head: Tuple[int, float] = (0, 0.0)


def _tie_substrate():
    global _tie_subtensor
    if not TIE_NETWORK:
        return get_subtensor().substrate
    if _tie_subtensor is None:
        import bittensor as bt

        _tie_subtensor = bt.Subtensor(network=TIE_NETWORK)
        logger.info(f"Connected to subtensor for tie blocks (network={TIE_NETWORK})")
    return _tie_subtensor.substrate


def _reset_tie() -> None:
    global _tie_subtensor
    if not TIE_NETWORK:
        reset_subtensor()
        return
    old, _tie_subtensor = _tie_subtensor, None
    if old is not None:
        try:
            old.substrate.close()
        except Exception:
            pass


def compact(data: bytes, i: int) -> Tuple[int, int]:
    """A SCALE compact integer at `i`, and the offset after it."""
    mode = data[i] & 3
    if mode == 0:
        return data[i] >> 2, i + 1
    if mode == 1:
        return int.from_bytes(data[i:i + 2], "little") >> 2, i + 2
    if mode == 2:
        return int.from_bytes(data[i:i + 4], "little") >> 2, i + 4
    n = (data[i] >> 2) + 4
    return int.from_bytes(data[i + 1:i + 1 + n], "little"), i + 1 + n


def inherent_calls(extrinsics) -> list:
    """(pallet index, call index, first argument as a compact) of each unsigned extrinsic."""
    out = []
    for xt in extrinsics:
        data = bytes.fromhex(str(xt)[2:] if str(xt).startswith("0x") else str(xt))
        _, i = compact(data, 0)
        if data[i] & 0x80 or (data[i] & 0x7F) not in (4, 5):
            continue
        try:
            out.append((data[i + 1], data[i + 2], compact(data, i + 3)[0]))
        except IndexError:
            continue
    return out


# (pallet index, call index) of Timestamp.set, found on the head block.
_set_call: Optional[Tuple[int, int]] = None


def _body_stamp(sub, block_hash) -> int:
    """A block's timestamp from its Timestamp.set inherent, read from the raw block body."""
    global _set_call
    if _set_call is None:
        head = sub.get_chain_finalised_head()
        now = int(sub.query("Timestamp", "Now", block_hash=head).value)
        found = [(p, c) for p, c, v in inherent_calls(_raw_extrinsics(sub, head)) if v == now]
        if len(found) != 1:
            raise RuntimeError("Timestamp.set not found in the head block")
        _set_call = found[0]
    got = [v for p, c, v in inherent_calls(_raw_extrinsics(sub, block_hash))
           if (p, c) == _set_call]
    if len(got) != 1:
        raise RuntimeError(f"Timestamp.set not found in block {block_hash}")
    return got[0]


def _raw_extrinsics(sub, block_hash) -> list:
    return sub.rpc_request("chain_getBlock", [block_hash])["result"]["block"]["extrinsics"]


def get_tie_block(after_ms: int) -> Optional[Tuple[int, str]]:
    """The first finalised block whose timestamp is at or after `after_ms`, as (number, hash);
    None until there is one."""
    global _tie_head
    if is_mock():
        return _mock_tie_block(after_ms)
    with _tie_lock:
        try:
            sub = _tie_substrate()

            def stamp(n: int) -> int:
                if n not in _stamps:
                    if len(_stamps) >= TIE_STAMPS_MAX:
                        _stamps.clear()
                    h = sub.get_block_hash(n)
                    try:
                        at = int(sub.query("Timestamp", "Now", block_hash=h).value)
                    except Exception:
                        at = 0
                    if at <= 0:
                        # A pruned node keeps old block bodies but not their state; some
                        # answer the missing state with an error, some with 0.
                        at = _body_stamp(sub, h)
                    _stamps[n] = (at, str(h))
                return _stamps[n][0]

            head, read_at = _tie_head
            if time.monotonic() - read_at >= TIE_HEAD_CACHE_S:
                head = int(sub.get_block_number(sub.get_chain_finalised_head()))
                _tie_head = (head, time.monotonic())
            at = stamp(head)
            if at < after_ms:
                return None
            n = max(1, head - (at - after_ms) // 12_000)
            while n < head and stamp(n) < after_ms:
                n += 1
            while n > 1 and stamp(n - 1) >= after_ms:
                n -= 1
            stamp(n)
            return n, _stamps[n][1]
        except Exception as e:
            logger.warning(f"Could not read the tie block: {e}")
            _reset_tie()
            return None


async def tie_block(after_ms: int) -> Optional[Tuple[int, str]]:
    return await asyncio.to_thread(get_tie_block, after_ms)
