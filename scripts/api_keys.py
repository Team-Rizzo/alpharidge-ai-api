"""Operator tool for product API keys. Only the sha256 of a key is stored.

    python scripts/api_keys.py add <owner>
    python scripts/api_keys.py revoke <owner>        every key of the owner
    python scripts/api_keys.py revoke --key <key>    one key
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.store import PrismaStore  # noqa: E402


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def run(store, args) -> str:
    if args.cmd == "add":
        key = secrets.token_urlsafe(32)
        await store.add_api_key(_hash(key), args.owner)
        return key
    n = await store.revoke_api_keys(owner_id=args.owner,
                                    key_hash=_hash(args.key) if args.key else None)
    return f"revoked {n}"


async def main_async(args) -> None:
    from prisma import Prisma

    db = Prisma()
    await db.connect()
    try:
        print(await run(PrismaStore(db), args))
    finally:
        await db.disconnect()


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add").add_argument("owner")
    rv = sub.add_parser("revoke")
    rv.add_argument("owner", nargs="?")
    rv.add_argument("--key")
    args = p.parse_args(argv)
    if args.cmd == "revoke" and not (args.owner or args.key):
        p.error("revoke needs an owner or --key")
    return args


def main() -> None:
    asyncio.run(main_async(parse()))


if __name__ == "__main__":
    main()
