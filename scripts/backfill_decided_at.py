"""Fill claim.decided_at for claims decided before the column existed (or decided by an older
API after the migration ran). Safe to run while the API (old or new) is live, to stop at any
point and to run again: it only writes claims whose decided_at is still NULL, in short
transactions over ranges of ids, and it leaves updated_at alone (the claim_touch trigger keeps
it when decided_at is the only change), so miners' change feeds do not replay.

    python scripts/backfill_decided_at.py              # fill every range
    python scripts/backfill_decided_at.py --check      # count claims still to fill; exit 1 if any
    python scripts/backfill_decided_at.py --from-id N  # resume where a stopped run said

Options: --batch (ids per transaction, default 2000), --pause (seconds between batches,
default 0.02), --vacuum-every (VACUUM claim after this many claims filled, so later batches
reuse the space of earlier ones and the table does not double; default 100000, 0 for never).
DATABASE_URL comes from the environment or the repo's .env, as for the API.

A rejected claim was decided when it was judged; a paid one (provisional, settled, clawed back)
when its provisional ledger row was written, else when it was judged. A decided claim with
neither time stays NULL (and is not counted by quality); --check lists those separately.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  (loads .env)

# The same statuses as DECIDED in app/services/store/rows.py.
PAID = "('provisional', 'settled', 'clawed_back')"
FIRST_PAID = ('(SELECT min(l."created_at") FROM "ledger_entry" l '
              'WHERE l."claim_id" = c."id" AND l."kind" = \'provisional\')')
# Claims to fill: decided, still NULL, and with a time to fill in (so no write is a no-op).
TODO = (f'c."decided_at" IS NULL AND ((c."status" = \'rejected\' AND c."judged_at" IS NOT NULL) '
        f'OR (c."status" IN {PAID} AND (c."judged_at" IS NOT NULL OR {FIRST_PAID} IS NOT NULL)))')
FILL = (f'UPDATE "claim" c SET "decided_at" = CASE WHEN c."status" = \'rejected\' '
        f'THEN c."judged_at" ELSE COALESCE({FIRST_PAID}, c."judged_at") END '
        f'WHERE c."id" >= $1::bigint AND c."id" < $2::bigint AND {TODO}')
# Each batch waits at most this long for a row another transaction holds, then is retried.
LOCK_TIMEOUT = "2s"
RETRIES = 10


async def check(db) -> int:
    rows = await db.query_raw(
        f'SELECT c."status", count(*) FILTER (WHERE {TODO}) AS todo, '
        f'count(*) FILTER (WHERE c."decided_at" IS NULL AND NOT ({TODO})) AS no_time, '
        f'count(*) FILTER (WHERE c."decided_at" IS NOT NULL) AS done FROM "claim" c '
        f'WHERE c."status" = \'rejected\' OR c."status" IN {PAID} GROUP BY 1 ORDER BY 1')
    todo = 0
    for r in rows:
        todo += int(r["todo"])
        print(f'{r["status"]:12} to fill {int(r["todo"]):>10}  done {int(r["done"]):>10}  '
              f'no time to fill {int(r["no_time"]):>8}')
    print(f"{todo} claims still to fill")
    return todo


async def fill_range(db, lo: int, hi: int) -> int:
    for attempt in range(1, RETRIES + 1):
        try:
            async with db.tx(max_wait=timedelta(seconds=10), timeout=timedelta(seconds=60)) as tx:
                await tx.execute_raw(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                await tx.execute_raw("SET LOCAL statement_timeout = '30s'")
                return await tx.execute_raw(FILL, lo, hi)
        except Exception as e:  # prisma reports the Postgres error only in its message
            if "55P03" not in str(e) and "lock timeout" not in str(e) or attempt == RETRIES:
                raise
            print(f"  ids {lo}..{hi - 1}: rows locked, retry {attempt}", flush=True)
            await asyncio.sleep(min(attempt, 5))
    raise AssertionError("unreachable")


async def vacuum(db) -> None:
    t = time.monotonic()
    await db.execute_raw('VACUUM "claim"')
    print(f"  VACUUM claim: {time.monotonic() - t:.1f} s", flush=True)


async def fill(db, start: int | None, batch: int, pause: float, vacuum_every: int) -> None:
    (b,) = await db.query_raw('SELECT min("id") AS lo, max("id") AS hi FROM "claim"')
    if b["lo"] is None:
        print("no claims")
        return
    lo, hi = int(start if start is not None else b["lo"]), int(b["hi"])
    # Claims made after this run started are stamped by the API that made them.
    first, total, t0, shown, since_vacuum = lo, 0, time.monotonic(), 0.0, 0
    try:
        while lo <= hi:
            n = await fill_range(db, lo, lo + batch)
            total, since_vacuum, lo = total + n, since_vacuum + n, lo + batch
            if vacuum_every and since_vacuum >= vacuum_every:
                await vacuum(db)
                since_vacuum = 0
            now = time.monotonic()
            if now - shown >= 5 or lo > hi:
                shown = now
                part = (min(lo, hi + 1) - first) / max(hi + 1 - first, 1)
                eta = (now - t0) / part * (1 - part) if part else 0
                print(f"ids {first}..{min(lo, hi + 1) - 1} of ..{hi}: {part:6.1%}, "
                      f"{total} filled, {now - t0:.0f} s, about {eta:.0f} s left", flush=True)
            if pause:
                await asyncio.sleep(pause)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"stopped after {total} filled; resume with --from-id {lo}")
        raise
    print(f"done: {total} claims filled in {time.monotonic() - t0:.0f} s; "
          "run with --check to confirm")


async def main_async(args) -> int:
    from prisma import Prisma

    url = os.getenv("DATABASE_URL")
    # VACUUM of a large claim table can take minutes.
    db = Prisma(http={"timeout": 3600}, **({"datasource": {"url": url}} if url else {}))
    await db.connect()
    try:
        if args.check:
            return 1 if await check(db) else 0
        await fill(db, args.from_id, args.batch, args.pause, args.vacuum_every)
        return 0
    finally:
        await db.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="count claims still to fill, change nothing")
    p.add_argument("--from-id", type=int, help="start at this claim id (resume a stopped run)")
    p.add_argument("--batch", type=int, default=2000, help="claim ids per transaction")
    p.add_argument("--pause", type=float, default=0.02, help="seconds between batches")
    p.add_argument("--vacuum-every", type=int, default=100_000,
                   help="VACUUM claim after this many claims filled (0: never)")
    args = p.parse_args()
    if args.batch < 1:
        p.error("--batch must be at least 1")
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
