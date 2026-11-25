"""Queue existing published events for enrichment. A dry run (the default) only counts them and
estimates the cost; --apply adds backfill rows, which the API's enrichment worker takes after
live events and only while the day's spend is under EL_ENRICH_BACKFILL_DAILY_USD. No model is
called here. Safe to run again: events that have a row are left alone.

    python scripts/enrich_backfill.py                    # count and estimate
    python scripts/enrich_backfill.py --apply            # queue them
    python scripts/enrich_backfill.py --since 2026-11-01 # only events published since then

DATABASE_URL comes from the environment or the repo's .env, as for the API.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  (loads .env)
from app.services import enrich  # noqa: E402
from app.services.store import PrismaStore  # noqa: E402

# Per event: the summary call and the embedding, at 24 Nov 2026 prices.
EST_USD = 0.00021
BATCH = 2000


async def run(store, since_ms: int, apply: bool, batch: int = BATCH) -> dict:
    now = time.time_ns() // 1_000_000
    if not apply:
        found = await store.enrich_candidates((enrich.CONFIRMED, enrich.REPORTED), since_ms,
                                              now, 10_000_000)
        yes = sum([await enrich.qualifies(store, ev, owner) for ev, owner in found])
        return {"qualifying": yes, "not_qualifying": len(found) - yes,
                "est_usd": round(yes * EST_USD, 2), "applied": False}
    queued = skipped = 0
    while True:
        q, s = await enrich.discover(store, now, since_ms=since_ms, limit=batch, backfill=True)
        queued, skipped = queued + q, skipped + s
        if not q and not s:
            break
        print(f"queued {queued}, skipped {skipped}", flush=True)
    return {"qualifying": queued, "not_qualifying": skipped,
            "est_usd": round(queued * EST_USD, 2), "applied": True}


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


async def main_async(args) -> None:
    from prisma import Prisma

    db = Prisma()
    await db.connect()
    try:
        print(await run(PrismaStore(db), _ms(args.since) if args.since else 0, args.apply,
                        args.batch))
    finally:
        await db.disconnect()


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="queue the events (default: dry run)")
    p.add_argument("--since", help="only events published from this UTC date (YYYY-MM-DD)")
    p.add_argument("--batch", type=int, default=BATCH)
    return p.parse_args(argv)


def main() -> None:
    asyncio.run(main_async(parse()))


if __name__ == "__main__":
    main()
