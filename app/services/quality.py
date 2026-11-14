"""Coldkey quality from judged claims (alpharidge_ai.mechanism.quality), read again at most
once a minute."""

import time
from typing import Dict, List, Optional, Tuple

from alpharidge_ai.mechanism.quality import JUNK, multiplier, scores
from app.config import EVENT_LANE
from app.services.store import Store

CACHE_S = 60
# An epoch's outcomes are kept for this long, so a later status change (a clawback, a scoop
# confirmed) cannot give the second validator another answer than the first.
EPOCH_CACHE_S = 3 * 3600

_cache: Dict[Tuple[int, Optional[int]], Tuple[float, List[Tuple[str, bool]]]] = {}


async def outcomes(store: Store, from_ms: Optional[int] = None,
                   until_ms: Optional[int] = None) -> List[Tuple[str, bool]]:
    """(owner, passed) for each paid or junk claim judged from from_ms (default
    QUALITY_FROM_MS) and before until_ms, in judged order."""
    key = (EVENT_LANE["QUALITY_FROM_MS"] if from_ms is None else int(from_ms), until_ms)
    at, rows = _cache.get(key, (0.0, None))
    if rows is None or time.monotonic() - at >= _ttl(key):
        rows = await store.quality_outcomes(key[0], JUNK, until_ms)
        for k in [k for k in _cache if k != key]:
            if time.monotonic() - _cache[k][0] >= _ttl(k):
                del _cache[k]
        _cache[key] = (time.monotonic(), rows)
    return rows


def _ttl(key: Tuple[int, Optional[int]]) -> float:
    return CACHE_S if key[1] is None else EPOCH_CACHE_S


async def multipliers(store: Store) -> Dict[str, float]:
    return {owner: multiplier(q) for owner, q in scores(await outcomes(store)).items()}


async def standing(store: Store) -> Dict[str, dict]:
    """Each scored coldkey's q, multiplier and counted claims."""
    rows = await outcomes(store)
    counts: Dict[str, List[int]] = {}
    for owner, passed in rows:
        counts.setdefault(owner, [0, 0])[0 if passed else 1] += 1
    return {owner: {"q": round(q, 4), "multiplier": round(multiplier(q), 4),
                    "paid": counts[owner][0], "junk": counts[owner][1]}
            for owner, q in scores(rows).items()}


def reset() -> None:
    _cache.clear()
