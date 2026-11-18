"""Operator tool for the source trust registry. Every change is written to the trust log.

    python scripts/trust.py seed [--catalogue app/data/catalogue.csv]
    python scripts/trust.py catalogue [--catalogue app/data/catalogue.csv] [--apply] --reason "..."
        (seed adds new sources only; this gives existing ones the catalogue's categories at the
        catalogue base, never lowering a base; a dry run unless --apply)
    python scripts/trust.py add example.com/ --trust 0.7 [--category finance] --reason "..."
    python scripts/trust.py add "*.example.com" --trust 0.7 --reason "..."  (host and subdomains)
    python scripts/trust.py set example.org --trust 0.8 --category finance --reason "..."
    python scripts/trust.py suspend example.com --reason "..."  (no clawback; a bare host covers
        its path rules too, as does ban)
    python scripts/trust.py ban example.com --reason "..."  (claws back open prizes)
    python scripts/trust.py unban example.com [--paths] --reason "..."  (a bare host's own row;
        its suspended or banned path rules too with --paths)
    python scripts/trust.py rule "example.com/blogs/*" --kind exclude --reason "..."
    python scripts/trust.py why example.org
    python scripts/trust.py review [--days 7]
    python scripts/trust.py nightly
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import shlex
import sys
import time
from typing import NamedTuple
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


async def set_statuses(store, text: str, cmd: str, reason: str, actor: str,
                       paths: bool = False) -> None:
    """Suspend, ban or unban the rows a text names. Unban on a bare host changes its own row
    only, unless `paths`, so path rules blocked on their own stay blocked."""
    status = {"suspend": tr.SUSPENDED, "ban": tr.BANNED}.get(cmd, tr.PENDING)
    rows = await tr.find_all(store, text)
    if not rows:
        sys.exit(f"unknown source: {text}")
    kept = []
    if cmd == "unban" and not tr.parse_source(text)[2]:
        blocked = [r for r in rows if r.path_rule and r.status in tr.BLOCKED]
        rows = [r for r in rows if not r.path_rule] + (blocked if paths else [])
        kept = [] if paths else blocked
    for row in rows:
        s = await tr.set_status(store, row, status, reason, actor)
        print(f"{tr.label(s)} {s.status}")
    for row in kept:
        print(f"{tr.label(row)} {row.status} (unchanged; unban it by its path or with --paths)")
    if not rows and not kept:
        print(f"nothing to unban on {text}")


class CategoryChange(NamedTuple):
    source: str
    category: str
    base: float


async def apply_catalogue(store, catalogue: Path, reason: str, actor: str,
                          apply: bool = False) -> list:
    """Give each catalogue source already in the registry the catalogue base in every category
    the catalogue lists for it that has no trust row (a category it was seeded without). A
    category with a row is never touched, so a base an operator set stays as set. Prints what
    it adds, with the matching `set` command; changes nothing unless `apply`. Blocked sources
    and sources not in the registry are reported and left alone."""
    changes = []
    for domain, path, cats in tr.read_catalogue(catalogue):
        text = domain + path
        source = await tr.find(store, text)
        if source is None:
            print(f"{text}: not in the registry (seed adds it)")
            continue
        if source.status in tr.BLOCKED:
            print(f"{text}: {source.status}, skipped")
            continue
        have = {r.category for r in await store.source_trust(source.id)}
        todo = [c for c in cats if c not in have
                and await tr.base_of(store, source, c) < tr.TRUST_BASE_CATALOGUE]
        for c in todo:
            base = await tr.base_of(store, source, c)
            changes.append(CategoryChange(text, c, base))
            print(f"{text} {c}: no trust row (falls back to {base:.2f}); "
                  f"{'added' if apply else 'would add'} it at {tr.TRUST_BASE_CATALOGUE:.2f}"
                  f"{'' if apply else ' (dry run)'}")
            print(f"  python scripts/trust.py set {shlex.quote(text)} --trust "
                  f"{tr.TRUST_BASE_CATALOGUE:g} --category {c} --reason {shlex.quote(reason)}")
        if apply and todo:
            await tr.set_trust(store, source, tr.TRUST_BASE_CATALOGUE, reason, actor, todo)
    print(f"{len(changes)} missing categories "
          f"{'added' if apply else 'to add (run with --apply)'}")
    return changes


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
        elif args.cmd == "catalogue":
            await apply_catalogue(store, args.catalogue, args.reason, actor, args.apply)
        elif args.cmd == "add":
            s = await tr.add(store, args.source, args.trust, args.reason, actor, args.category)
            print(f"{tr.label(s)} {s.category_trust}")
        elif args.cmd == "set":
            s = await tr.set_trust(store, await _source(store, args.source), args.trust,
                                   args.reason, actor, args.category)
            print(f"{tr.label(s)} {s.category_trust}")
        elif args.cmd in ("suspend", "ban", "unban"):
            await set_statuses(store, args.source, args.cmd, args.reason, actor,
                               getattr(args, "paths", False))
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
    c = change("catalogue")
    c.add_argument("--catalogue", type=Path, default=tr.CATALOGUE)
    c.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    for name in ("add", "set"):
        c = change(name, "source")
        c.add_argument("--trust", type=float, required=True)
        c.add_argument("--category", action="append", default=[])
    change("suspend", "source")
    change("ban", "source")
    change("unban", "source").add_argument(
        "--paths", action="store_true", help="also its suspended or banned path rules")
    r = change("rule", "pattern")
    r.add_argument("--kind", choices=("exclude", "allow", "listing", "wire"), default="exclude")
    sub.add_parser("why").add_argument("source")
    sub.add_parser("review").add_argument("--days", type=float, default=7)
    sub.add_parser("nightly")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
