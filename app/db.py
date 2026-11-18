"""Shared Prisma clients."""

import logging
import os
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from prisma import Prisma

from app import config

logger = logging.getLogger(__name__)


def pooled_url(url: str, limit: str = "", timeout_s: str = "") -> Optional[str]:
    """The database URL with this process's connection limit and pool timeout, or None when
    neither is set."""
    extra = {k: v for k, v in (("connection_limit", limit), ("pool_timeout", timeout_s)) if v}
    if not url or not extra:
        return None
    parts = urlsplit(url)
    query = {**dict(parse_qsl(parts.query)), **extra}
    return urlunsplit(parts._replace(query=urlencode(query)))


# Prisma waits this long for a pooled connection (pool_timeout, 10 s unless
# EL_DB_POOL_TIMEOUT_S sets it) before a query fails.
POOL_TIMEOUT_MS = int(float(config.DB_POOL_TIMEOUT_S or 10) * 1000)
# A transaction waits for its connection as long as a plain query does, plus a margin for its
# BEGIN: Prisma's 2 s default failed a pay's transaction under pool pressure while plain
# queries still went through.
TX_MAX_WAIT_MS = POOL_TIMEOUT_MS + 5000
# Longest a transaction may stay open (Prisma's default is 5 s).
TX_TIMEOUT_MS = 60_000
# Longest a transaction waits for the ledger lock. The exclusive hold only seals an epoch (a
# few rows), and every shared hold is one short write, so a wait this long means something is
# stuck: the transaction fails and rolls back instead of holding its pooled connection until
# TX_TIMEOUT_MS, and the claim stays where it was. Pays and settles are done again by the
# resolver and settle passes; of the jobs, only confirm and re-check jobs are retried
# (app/jobs/worker.py RETRIED).
LEDGER_LOCK_TIMEOUT_MS = 20_000

_url = pooled_url(os.getenv("DATABASE_URL", ""), config.DB_CONNECTION_LIMIT,
                  config.DB_POOL_TIMEOUT_S)
# Must exceed the slowest raw dashboard query.
prisma = Prisma(http={"timeout": 180}, **({"datasource": {"url": _url}} if _url else {}))


async def connect() -> None:
    await prisma.connect()
    logger.info("Connected to database" + (f" ({config.DB_CONNECTION_LIMIT} connections)"
                                           if config.DB_CONNECTION_LIMIT else ""))


async def disconnect() -> None:
    await prisma.disconnect()
    logger.info("Disconnected from database")
