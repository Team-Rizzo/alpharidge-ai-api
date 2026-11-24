"""One-off cleanup of events split before novelty matched a claim's page first: events of one
type and date that claims on the same page (not a listing page) are on are merged into the
earliest, and lines whose claims were all rejected are deleted. Dry run by default.

    python scripts/merge_page_events.py              # counts and the first groups, no writes
    python scripts/merge_page_events.py --apply      # merge and delete
    python scripts/merge_page_events.py --days 30    # only pages claimed in the last 30 days

A merged event's claims, source records and answer-key matches move to the kept event. A merged
event with ledger rows (the ledger cannot change) stays as a hidden line with no claims; one
without is deleted with the dead lines. Groups holding a scoop still waiting for a trusted page
(confidence unverified) are left alone. Moved claims show again in their miners' change feeds.
DATABASE_URL comes from the environment or the repo's .env, as for the API.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  (loads .env)

GROUPS = (
    'SELECT c."source_url" AS url, array_agg(DISTINCT e."id") AS ids, '
    'bool_or(e."confidence" = \'unverified\') AS held FROM "claim" c '
    'JOIN "events" e ON e."id" = c."event_id" '
    'WHERE c."status" <> \'rejected\' AND e."confidence" <> \'line\' '
    'AND c."received_at" >= to_timestamp($1::double precision / 1000) '
    'GROUP BY c."source_url", e."event_type", e."event_date" HAVING count(DISTINCT e."id") > 1')
MOVES = (
    'UPDATE "claim" SET "event_id" = $1 WHERE "event_id" = $2',
    'UPDATE "claim" SET "pending_of_event_id" = $1 WHERE "pending_of_event_id" = $2',
    'UPDATE "source_event" SET "event_id" = $1 WHERE "event_id" = $2',
    'UPDATE "answer_key_item" SET "matched_event_id" = $1 WHERE "matched_event_id" = $2',
    'UPDATE "events" SET "confidence" = \'line\', "first_claim_id" = NULL, "publish_at" = NULL '
    'WHERE "id" = $2',
)


def merges(groups: Iterable[Sequence[int]]) -> Dict[int, List[int]]:
    """Kept event -> the events merged into it. Groups sharing an event are one merge, kept at
    its earliest event."""
    parent: Dict[int, int] = {}

    def root(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ids in groups:
        ids = sorted(ids)
        for i in ids[1:]:
            a, b = root(ids[0]), root(i)
            if a != b:
                parent[max(a, b)] = min(a, b)
    out: Dict[int, List[int]] = {}
    for x in sorted(parent):
        r = root(x)
        if r != x:
            out.setdefault(r, []).append(x)
    return out


async def plan(db, registry, since_ms: int):
    rows = await db.query_raw(GROUPS, since_ms)
    listing = sum(registry.is_listing(r["url"]) for r in rows)
    held = sum(bool(r["held"]) for r in rows if not registry.is_listing(r["url"]))
    groups = [[int(i) for i in r["ids"]] for r in rows
              if not registry.is_listing(r["url"]) and not r["held"]]
    # A held group's events are left out of every merge.
    skip: Set[int] = {int(i) for r in rows if r["held"] for i in r["ids"]}
    return merges([g for g in groups if not skip & set(g)]), len(rows), listing, held


async def main_async(args) -> int:
    from prisma import Prisma

    from app.services import line, product, trust
    from app.services.entities import shared_index
    from app.services.store.prisma import PrismaStore

    url = os.getenv("DATABASE_URL")
    db = Prisma(http={"timeout": 600}, **({"datasource": {"url": url}} if url else {}))
    await db.connect()
    try:
        store = PrismaStore(db)
        registry = trust.TrustRegistry()
        await registry.refresh(store)
        now_ms = int(time.time() * 1000)
        todo, pages, listing, held = await plan(db, registry, now_ms - args.days * 86_400_000)
        merged = sum(len(v) for v in todo.values())
        print(f"pages with more than one event: {pages} ({listing} listing pages and {held} "
              f"with a held scoop left alone)")
        print(f"merges: {len(todo)} kept events, {merged} events merged into them")
        for kept, dups in list(todo.items())[:args.show]:
            print(f"  {kept} <- {', '.join(map(str, dups))}")
        dead = await store.dead_lines(now_ms - line.DEAD_LINE_H * 3_600_000, 10_000_000)
        print(f"dead lines: {len(dead)}")
        if not args.apply:
            print("dry run: nothing changed (run with --apply)")
            return 0
        for kept, dups in todo.items():
            async with db.tx() as tx:
                for dup in dups:
                    for sql in MOVES:
                        await tx.execute_raw(sql, kept, dup)
        await product.sync_events(store, todo, now_ms, shared_index())
        dropped = await store.drop_lines([d for dups in todo.values() for d in dups])
        while dead:
            dropped += await store.drop_lines(dead[:1000])
            dead = dead[1000:]
        print(f"merged {merged} events into {len(todo)}; deleted {dropped} dead lines")
        return 0
    finally:
        await db.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="merge and delete (default: dry run)")
    p.add_argument("--days", type=int, default=3650, help="pages claimed in the last N days")
    p.add_argument("--show", type=int, default=20, help="merges to list")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
