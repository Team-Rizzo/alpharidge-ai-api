"""
TAO Price History Service.

Fetches TAO/USD price from TaoStats API every 15 minutes and writes it to the database.
This runs as a separate background task to maintain historical price data.

Supports a separate PRICE_DATABASE_URL for writing price data to a different database.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from prisma import Prisma

logger = logging.getLogger(__name__)

# Configuration from environment
TAO_PRICE_HISTORY_REFRESH_SECONDS = int(os.getenv("TAO_PRICE_HISTORY_REFRESH_SECONDS", "900"))  # 15 minutes
TAOSTATS_API_URL = os.getenv("TAOSTATS_API_URL", "https://api.taostats.io/api/price/latest/v1?asset=tao")
TAOSTATS_API_KEY = os.getenv("TAOSTATS_API_KEY", "")
TAOSTATS_TIMEOUT = float(os.getenv("TAOSTATS_TIMEOUT", "10.0"))

# Optional separate database for price data
PRICE_DATABASE_URL = os.getenv("PRICE_DATABASE_URL", "")

# Global state
_prisma: Optional[Prisma] = None
_own_prisma: bool = False  # True if we created our own Prisma client
_history_task: Optional[asyncio.Task] = None


async def connect_price_database() -> None:
    """
    Connect to the price database.
    
    If PRICE_DATABASE_URL is set, creates a separate Prisma client for that database.
    Otherwise, you must call set_prisma_client() to use a shared client.
    """
    global _prisma, _own_prisma
    
    if PRICE_DATABASE_URL:
        _prisma = Prisma(datasource={"url": PRICE_DATABASE_URL})
        await _prisma.connect()
        _own_prisma = True
        # Log host only, not credentials
        db_host = PRICE_DATABASE_URL.split('@')[-1] if '@' in PRICE_DATABASE_URL else "configured database"
        logger.info(f"Connected to price database: {db_host}")
    else:
        logger.info("No PRICE_DATABASE_URL set, use set_prisma_client() to provide a shared client")


async def disconnect_price_database() -> None:
    """Disconnect from the price database if we own the connection."""
    global _prisma, _own_prisma
    
    if _own_prisma and _prisma is not None:
        await _prisma.disconnect()
        logger.info("Disconnected from price database")
        _prisma = None
        _own_prisma = False


def set_prisma_client(prisma: Prisma) -> None:
    """Set the Prisma client for database writes. Call this if not using PRICE_DATABASE_URL."""
    global _prisma, _own_prisma
    
    if PRICE_DATABASE_URL:
        logger.warning("PRICE_DATABASE_URL is set, ignoring set_prisma_client() call")
        return
    
    _prisma = prisma
    _own_prisma = False
    logger.info("Prisma client set for TAO price history service")


async def fetch_tao_price_from_api() -> float:
    """
    Fetch TAO/USD price from TaoStats API.
    
    Returns:
        The TAO price in USD.
        
    Raises:
        Exception if fetch fails.
    """
    headers = {}
    if TAOSTATS_API_KEY:
        headers["Authorization"] = TAOSTATS_API_KEY
    
    async with httpx.AsyncClient(timeout=TAOSTATS_TIMEOUT) as client:
        response = await client.get(TAOSTATS_API_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        # Parse the response - handle various possible response formats
        # Try different common response structures
        price = None
        
        # Direct price field
        if "price" in data:
            price = data["price"]
        # Nested data.price
        elif "data" in data and isinstance(data["data"], dict):
            if "price" in data["data"]:
                price = data["data"]["price"]
        # Array format with price field
        elif "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            first_item = data["data"][0]
            if isinstance(first_item, dict):
                price = first_item.get("price") or first_item.get("value")
        # Try other common field names
        elif "value" in data:
            price = data["value"]
        elif "usd" in data:
            price = data["usd"]
        elif "usdPrice" in data:
            price = data["usdPrice"]
        
        if price is None:
            raise ValueError(f"Could not find price in API response: {data}")
        
        return float(price)


async def fetch_and_write_price() -> None:
    """Fetch TAO price from API and write it to the database."""
    global _prisma
    
    if _prisma is None:
        logger.warning("Prisma client not set, skipping price history write")
        return
    
    max_retries = 3
    retry_delays = [1, 2, 4]  # Exponential backoff
    
    for attempt in range(max_retries):
        try:
            price = await fetch_tao_price_from_api()
            timestamp = datetime.now(timezone.utc)
            
            # Write to database
            await _prisma.taousdprice.create(
                data={
                    "taoPrice": price,
                    "date": timestamp,
                }
            )
            
            logger.info(f"TAO price written to database: ${price:.2f} at {timestamp.isoformat()}")
            return
            
        except Exception as e:
            if attempt < max_retries - 1:
                delay = retry_delays[attempt]
                logger.warning(f"TAO price fetch/write failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay}s...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"TAO price fetch/write failed after {max_retries} attempts: {e}")


async def _history_loop() -> None:
    """Background loop that fetches and writes price every TAO_PRICE_HISTORY_REFRESH_SECONDS."""
    # Wait a bit on startup to let the app fully initialize
    await asyncio.sleep(5)
    
    while True:
        try:
            await fetch_and_write_price()
        except Exception as e:
            logger.error(f"Unexpected error in price history loop: {e}")
        
        await asyncio.sleep(TAO_PRICE_HISTORY_REFRESH_SECONDS)


def start_history_task() -> asyncio.Task:
    """Start the background price history task. Call this at app startup."""
    global _history_task
    if _history_task is None or _history_task.done():
        _history_task = asyncio.create_task(_history_loop())
        logger.info(f"TAO price history task started (interval: {TAO_PRICE_HISTORY_REFRESH_SECONDS}s)")
    return _history_task


def stop_history_task() -> None:
    """Stop the background price history task. Call this at app shutdown."""
    global _history_task
    if _history_task and not _history_task.done():
        _history_task.cancel()
        logger.info("TAO price history task stopped")


