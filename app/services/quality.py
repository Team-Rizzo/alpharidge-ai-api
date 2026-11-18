"""Coldkey quality from decided claims (alpharidge_ai.mechanism.quality), read again at most
once a minute."""

import asyncio
import time
from typing import Dict, List, Optional, Tuple

from alpharidge_ai.mechanism.quality import JUNK, multiplier, pay_multiplier, scores
from app.config import EVENT_LANE
from app.services.store import Store
from app.utils.clock import DAY_MS, HOUR_MS, now_ms as _now_ms

CACHE_S = 60
# An epoch's outcomes are kept this long. They do not change once the epoch is signed (claims
# are read by when they were decided), so this only spares the database.
EPOCH_CACHE_S = 3 * 3600
# With no start set (QUALITY_FROM_MS 0), the budget and the dashboard score the claims decided
# in this span instead of every claim ever. q halves a claim's weight every 20 claims after it:
# a coldkey that decides 100 claims in the span is scored to within 2^-5 of its full history,
# and a quieter one starts the span at START, as a new coldkey does.
LOOKBACK_MS = 30 * DAY_MS

# Most reads kept at once; the oldest goes first.
CACHE_MAX = 32

Key = Tuple[int, Optional[int]]
_cache: Dict[Key, Tuple[float, List[Tuple[str, bool, int]]]] = {}
_loading: Dict[Key, asyncio.Future] = {}


def start() -> int:
    """Where the budget and the dashboard start: QUALITY_FROM_MS, else the lookback, to the
    hour so that reads share the cache."""
    if EVENT_LANE["QUALITY_FROM_MS"]:
        return EVENT_LANE["QUALITY_FROM_MS"]
    cut = _now_ms() - LOOKBACK_MS
    return cut - cut % HOUR_MS


async def outcomes(store: Store, from_ms: Optional[int] = None,
                   until_ms: Optional[int] = None) -> List[Tuple[str, bool, int]]:
    """(owner, passed, claim id) for each paid or junk claim decided from from_ms (default
    start()) and before until_ms, in decision order. One read runs per key at a time; other
    callers wait for it."""
    key = (start() if from_ms is None else int(from_ms), until_ms)
    at, rows = _cache.get(key, (0.0, None))
    if rows is not None and time.monotonic() - at < _ttl(key):
        return rows
    task = _loading.get(key)
    if task is None:
        task = _loading[key] = asyncio.ensure_future(_load(store, key))

        def done(t: asyncio.Future) -> None:
            if _loading.get(key) is t:
                del _loading[key]

        task.add_done_callback(done)
    return await asyncio.shield(task)


async def _load(store: Store, key: Key) -> List[Tuple[str, bool, int]]:
    rows = await store.quality_outcomes(key[0], JUNK, key[1])
    now = time.monotonic()
    for k in [k for k in _cache if now - _cache[k][0] >= _ttl(k)]:
        del _cache[k]
    _cache.pop(key, None)
    while len(_cache) >= CACHE_MAX:
        del _cache[min(_cache, key=lambda k: _cache[k][0])]
    _cache[key] = (now, rows)
    return rows


def _ttl(key: Key) -> float:
    return CACHE_S if key[1] is None else EPOCH_CACHE_S


async def multipliers(store: Store) -> Dict[str, float]:
    return {owner: multiplier(q) for owner, q in scores(await outcomes(store)).items()}


async def standing(store: Store) -> Dict[str, dict]:
    """Each scored coldkey's q, the pay multiplier its next paid claim gets, its check-budget
    multiplier and counted claims."""
    rows = await outcomes(store)
    counts: Dict[str, List[int]] = {}
    for owner, passed, _ in rows:
        counts.setdefault(owner, [0, 0])[0 if passed else 1] += 1
    return {owner: {"q": round(q, 4), "pay_multiplier": round(pay_multiplier(q), 4),
                    "multiplier": round(multiplier(q), 4),
                    "paid": counts[owner][0], "junk": counts[owner][1]}
            for owner, q in scores(rows).items()}


def reset() -> None:
    _cache.clear()
    _loading.clear()
