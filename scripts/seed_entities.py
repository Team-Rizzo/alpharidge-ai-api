"""Load the entity alias table.

    python scripts/seed_entities.py load

`load` upserts the seed (the subnet package's `schema/data/entities.json`) into Entity.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import entities as ent  # noqa: E402


async def load(seed: Path) -> None:
    from prisma import Prisma

    db = Prisma()
    await db.connect()
    try:
        n = await ent.seed_entities(db, ent.read_seed(seed))
        print(f"entities: {n}")
    finally:
        await db.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    ld = sub.add_parser("load")
    ld.add_argument("--seed", type=Path, default=ent.SEED_PATH)
    args = p.parse_args()
    asyncio.run(load(args.seed))


if __name__ == "__main__":
    main()
