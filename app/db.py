"""Shared Prisma clients."""

import logging

from prisma import Prisma

logger = logging.getLogger(__name__)

# Must exceed the slowest raw dashboard query.
prisma = Prisma(http={"timeout": 180})


async def connect() -> None:
    await prisma.connect()
    logger.info("Connected to database")


async def disconnect() -> None:
    await prisma.disconnect()
    logger.info("Disconnected from database")

