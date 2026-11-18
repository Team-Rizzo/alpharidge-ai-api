"""Delete the event ledger's unsigned seals before the old (pre-seal) API writer starts on a
rollback. A seal is the attestation row the new writer makes before it signs an epoch, with no
capacity and an empty root and signature; the old code cannot fill one and mishandles its epoch.
Run it with the new writer stopped and before the old writer starts (DEPLOY.md, Rollback).

    python scripts/clear_seals.py            # dry run: lists the seals it would delete
    python scripts/clear_seals.py --apply    # deletes them

It deletes in one transaction under the ledger lock taken exclusively, so it never runs beside
a seal or a ledger write. The epochs' carry rows stay: the old writer sees them and signs the
epoch with them. DATABASE_URL comes from the environment or the repo's .env, as for the API.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  (loads .env)

SIGNER = "event-ledger"
# The same key as app/services/store/prisma.py _LEDGER_LOCK.
LEDGER_LOCK = 4545002
SEALS = ("FROM attestation WHERE validator_hotkey = $1 AND capacity IS NULL "
         "AND merkle_root = '' AND signature = ''")


async def run(db, apply: bool) -> int:
    rows = await db.query_raw(
        f"SELECT epoch, issued_at::text AS issued_at {SEALS} ORDER BY epoch", SIGNER)
    for r in rows:
        print(f"seal: epoch {r['epoch']} sealed at {r['issued_at']}")
    print(f"{len(rows)} unsigned seals")
    if not apply:
        print(f"dry run; --apply runs: DELETE {SEALS.replace('$1', repr(SIGNER))}")
        return 0
    async with db.tx() as tx:
        await tx.query_raw(f"SELECT 1 AS ok FROM pg_advisory_xact_lock({LEDGER_LOCK})")
        n = await tx.execute_raw(f"DELETE {SEALS}", SIGNER)
    print(f"deleted {n} seals")
    return 0


async def main_async(args) -> int:
    from prisma import Prisma

    url = os.getenv("DATABASE_URL")
    db = Prisma(**({"datasource": {"url": url}} if url else {}))
    await db.connect()
    try:
        return await run(db, args.apply)
    finally:
        await db.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true",
                   help="delete the seals (only with the writer stopped)")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
