"""Operator tool for the source trust registry. Every change is written to the trust log.

    python scripts/trust.py seed [--catalogue app/data/catalogue.csv]
    python scripts/trust.py add example.com/ --trust 0.7 [--category finance] --reason "..."
    python scripts/trust.py add "*.example.com" --trust 0.7 --reason "..."  (host and subdomains)
    python scripts/trust.py set example.org --trust 0.8 --category finance --reason "..."
    python scripts/trust.py ban example.com --reason "..."
    python scripts/trust.py unban example.com --reason "..."
    python scripts/trust.py rule "example.com/blogs/*" --kind exclude --reason "..."
    python scripts/trust.py why example.org
    python scripts/trust.py review [--days 7]
    python scripts/trust.py nightly
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jobs import trust_nightly  # noqa: E402
from app.services import trust as tr  # noqa: E402
from app.services.store import PrismaStore  # noqa: E402
from app.utils.clock import DAY_MS  # noqa: E402


def _when(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _source(store, text):
    source = await tr.find(store, text)
    if source is None:
        sys.exit(f"unknown source: {text}")
    return source


async def why(store, text) -> None:
    s = await _source(store, text)
    owner = await store.get_owner(s.owner_id) if s.owner_id else None
    print(f"{tr.label(s)}  status={s.status}  base={s.base_trust}  owner={s.owner_id}")
    if owner is not None:
        print(f"  owner since {_when(owner.first_seen_ms)}")
    for r in await store.source_trust(s.id):
        print(f"  {r.category:10} trust={r.trust:.3f} ({tr.band(r.trust)})  base={r.base:.2f} "
              f"penalties={r.penalties:.3f}")
    for e in (await store.source_events(s.id))[-10:]:
        print(f"  {_when(e.at_ms)} {e.role:10} {e.category:10} event={e.event_id} "
              f"claim={e.claim_id}")
    for e in (await store.trust_log(source_id=s.id))[-10:]:
        print(f"  {_when(e.at_ms)} {e.actor}: {e.field} {e.old} -> {e.new} ({e.reason})")


async def review(store, days: float) -> None:
    since = time.time_ns() // 1_000_000 - int(days * DAY_MS)
    flags = [e for e in await store.trust_log(actor="detector", since_ms=since)
             if e.field.startswith("flag:")]
    labels = {s.id: tr.label(s) for s in await store.sources()}
    for e in flags:
        print(f"{_when(e.at_ms)} {e.field:14} {labels.get(e.source_id, e.source_id)}  "
              f"{e.old} -> {e.new} ({e.reason})")
    print(f"{len(flags)} flags in the last {days:g} days")


async def main_async(args) -> None:
    from prisma import Prisma

    db = Prisma()
    await db.connect()
    store = PrismaStore(db)
    actor = f"operator:{args.by}" if hasattr(args, "by") else "operator"
    try:
        if args.cmd == "seed":
            print(f"{await tr.seed(store, catalogue=args.catalogue)} sources added")
        elif args.cmd == "add":
            s = await tr.add(store, args.source, args.trust, args.reason, actor, args.category)
            print(f"{tr.label(s)} {s.category_trust}")
        elif args.cmd == "set":
            s = await tr.set_trust(store, await _source(store, args.source), args.trust,
                                   args.reason, actor, args.category)
            print(f"{tr.label(s)} {s.category_trust}")
        elif args.cmd in ("ban", "unban"):
            status = tr.BANNED if args.cmd == "ban" else tr.PENDING
            s = await tr.set_status(store, await _source(store, args.source), status,
                                    args.reason, actor)
            print(f"{tr.label(s)} {s.status}")
        elif args.cmd == "rule":
            r = await tr.add_rule(store, args.pattern, args.kind, args.reason, actor)
            print(f"rule {r.id}: {r.kind} {r.pattern}")
        elif args.cmd == "why":
            await why(store, args.source)
        elif args.cmd == "review":
            await review(store, args.days)
        elif args.cmd == "nightly":
            print(f"{await trust_nightly.run(store)} rows")
    finally:
        await db.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def change(name, *extra):
        c = sub.add_parser(name)
        for a in extra:
            c.add_argument(a)
        c.add_argument("--reason", required=True)
        c.add_argument("--by", default=getpass.getuser())
        return c

    s = sub.add_parser("seed")
    s.add_argument("--catalogue", type=Path)
    for name in ("add", "set"):
        c = change(name, "source")
        c.add_argument("--trust", type=float, required=True)
        c.add_argument("--category", action="append", default=[])
    change("ban", "source")
    change("unban", "source")
    r = change("rule", "pattern")
    r.add_argument("--kind", choices=("exclude", "allow", "listing", "wire"), default="exclude")
    sub.add_parser("why").add_argument("source")
    sub.add_parser("review").add_argument("--days", type=float, default=7)
    sub.add_parser("nightly")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
