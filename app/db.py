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
