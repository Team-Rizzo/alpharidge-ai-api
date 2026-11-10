"""Answer-key polling: each official feed at its own low frequency, then matching and Q."""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import EVENT_LANE
from app.services import verifier
from app.services.answer_key import accuracy, match, releases
from app.services.answer_key.base import AK_MATCH_H, FeedAdapter
from app.services.answer_key.bea import Bea
from app.services.answer_key.bls import Bls
from app.services.answer_key.edgar import Edgar
from app.services.answer_key.fed import Fed
from app.services.entities import shared_index
from app.services.store import Store
from app.utils.clock import HOUR_MS

AK_POLL_S = {"edgar": 600, "fed": 1800, "bls": 1800, "bea": 1800}
AK_TICK_S = 60
AK_MATCH_EVERY_S = 600

logger = logging.getLogger(__name__)

ADAPTERS = {a.name: a for a in (Edgar, Fed, Bls, Bea)}


def enabled_adapters(names: Sequence[str] = ()) -> List[FeedAdapter]:
    out = []
    for name in names or EVENT_LANE["AK_ADAPTERS"]:
        cls = ADAPTERS.get(name)
        if cls is None:
            logger.warning(f"Answer key: unknown adapter {name}")
            continue
        adapter = cls()
        if adapter.enabled():
            out.append(adapter)
        else:
            logger.warning(f"Answer key: adapter {name} is not configured; skipped")
    return out


def feed_types(adapters: Sequence[FeedAdapter]) -> Tuple[str, ...]:
    return tuple(sorted({t for a in adapters for t in a.types}))


async def poll(store: Store, adapter: FeedAdapter, now_ms: int) -> int:
    """Store the adapter's records that are recent enough to match."""
    cutoff = now_ms - int(AK_MATCH_H * HOUR_MS)
    rows = [r for r in await adapter.poll(now_ms) if (r.published_ms or now_ms) >= cutoff]
    for r in rows:
        r.observed_ms = now_ms
    if adapter.release_times:
        releases.get_book().add([*rows, *adapter.scheduled(now_ms)], now_ms)
    return len(await store.add_answer_items(rows))


class Poller:
    def __init__(self, adapters: Optional[Sequence[FeedAdapter]] = None):
        self.adapters = list(adapters) if adapters is not None else enabled_adapters()
        self._last: Dict[str, int] = {}
        self._matched_ms: Optional[int] = None

    def due(self, adapter: FeedAdapter, now_ms: int) -> bool:
        every = AK_POLL_S.get(adapter.name, 1800) * 1000
        return now_ms - self._last.get(adapter.name, -every) >= every

    async def run(self, store: Store, now_ms: int, head: Optional[int] = None) -> Dict[str, int]:
        added = {}
        for adapter in self.adapters:
            if not self.due(adapter, now_ms):
                continue
            self._last[adapter.name] = now_ms
            try:
                added[adapter.name] = await poll(store, adapter, now_ms)
            except Exception as e:
                logger.warning(f"Answer key: poll of {adapter.name} failed: {e!r}")
        every = AK_MATCH_EVERY_S * 1000
        recent = self._matched_ms is not None and now_ms - self._matched_ms < every
        if recent and not any(added.values()):
            return added
        self._matched_ms = now_ms
        v = verifier.get_verifier()
        if v.entities is None:
            v.entities = shared_index()
        await match.match_open(store, v.entities, v.places, now_ms, head)
        await accuracy.scan_outcomes(store, now_ms, feed_types(self.adapters))
        await accuracy.recompute(store, now_ms)
        gaps = await match.gap_alerts(store, now_ms)
        if gaps:
            logger.warning(f"Answer key: {len(gaps)} official records with no miner event")
        return added


async def run_forever(store: Store) -> None:
    poller = Poller()
    logger.info(f"Answer key adapters: {[a.name for a in poller.adapters]}")
    while True:
        try:
            added = await poller.run(store, time.time_ns() // 1_000_000)
            if any(added.values()):
                logger.info(f"Answer key: new items {added}")
        except Exception as e:
            logger.error(f"Answer key tick failed: {e!r}")
        await asyncio.sleep(AK_TICK_S)
