"""Nightly trust recompute: SourceTrust rows, the per-source cache, status and review flags."""

import asyncio
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import CATEGORIES, PRIVATE
from app.services import page_flags, trust
from app.services.store import Store
from app.utils.clock import DAY_MS

TRUST_NIGHTLY_HOUR_UTC = 3

logger = logging.getLogger(__name__)


async def run(store: Store, now_ms: Optional[int] = None) -> int:
    now_ms = now_ms or time.time_ns() // 1_000_000
    sources = await store.sources()
    bases = defaultdict(dict)
    for r in await store.source_trust():
        bases[r.source_id][r.category] = r.base
    states = [trust.SourceState(s, bases[s.id]) for s in sources]
    events = await store.source_events()
    rows = trust.nightly(states, events, now_ms, CATEGORIES)
    await store.put_source_trust(rows)

    by_source = defaultdict(dict)
    for r in rows:
        by_source[r.source_id][r.category] = r.trust
    for s in sources:
        cache = by_source.get(s.id, {})
        status = s.status
        if status in (trust.PENDING, trust.ACTIVE):
            best = max(cache.values(), default=0.0)
            status = trust.ACTIVE if best >= trust.PRIZE_TRUST_MIN else trust.PENDING
        await store.update_source(s.id, category_trust=cache, status=status)
        if status != s.status:
            await trust.log(store, "status", s.status, status, "nightly", "nightly", s.id,
                            now_ms=now_ms)
        for c, t in sorted(cache.items()):
            old = s.category_trust.get(c, s.base_trust)
            if trust.band(old) != trust.band(t):
                await trust.log(store, f"band:{c}", trust.band(old), trust.band(t), "nightly",
                                "nightly", s.id, now_ms=now_ms)
    await _flag_spikes(store, events, now_ms)
    await page_flags.run(store, now_ms)
    return len(rows)


async def _flag_spikes(store: Store, events, now_ms: int) -> None:
    base_days = PRIVATE["TRUST_SPIKE_BASE_DAYS"]
    day = Counter(e.source_id for e in events if now_ms - e.at_ms < DAY_MS)
    past = Counter(e.source_id for e in events
                   if DAY_MS <= now_ms - e.at_ms < (base_days + 1) * DAY_MS)
    for sid, n in sorted(day.items()):
        usual = past.get(sid, 0) / base_days
        if n >= PRIVATE["TRUST_SPIKE_MIN"] and n > PRIVATE["TRUST_SPIKE_X"] * usual:
            await trust.log(store, "flag:spike", round(usual, 2), n, "volume", "detector", sid,
                            now_ms=now_ms)


def _seconds_to_next_run(now: datetime) -> float:
    at = now.replace(hour=TRUST_NIGHTLY_HOUR_UTC, minute=0, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return (at - now).total_seconds()


async def run_daily(store: Store) -> None:
    while True:
        await asyncio.sleep(_seconds_to_next_run(datetime.now(timezone.utc)))
        try:
            n = await run(store)
            logger.info(f"Trust nightly: {n} rows")
        except Exception as e:
            logger.error(f"Trust nightly failed: {e!r}")
