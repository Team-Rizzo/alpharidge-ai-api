"""
TAO Price History Service.

Fetches TAO/USD price from TaoStats API or CoinGecko every 15 minutes and writes it to the database.
This runs as a separate background task to maintain historical price data.

Supports a separate PRICE_DATABASE_URL for writing price data to a different database.
Set USE_COINGECKO=true to use CoinGecko instead of TaoStats.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass

import httpx
from prisma import Prisma


@dataclass
class TaoPriceData:
    """Container for TAO price and market data."""
    price: float
    market_cap: Optional[float] = None
    volume_24h: Optional[float] = None
    price_change_24h: Optional[float] = None
    market_cap_change_24h: Optional[float] = None
    price_change_7d: Optional[float] = None
    price_change_30d: Optional[float] = None
    source: Optional[str] = None

logger = logging.getLogger(__name__)

# Configuration from environment
TAO_PRICE_HISTORY_REFRESH_SECONDS = int(os.getenv("TAO_PRICE_HISTORY_REFRESH_SECONDS", "900"))  # 15 minutes
TAOSTATS_API_URL = os.getenv("TAOSTATS_API_URL", "https://api.taostats.io/api/price/latest/v1?asset=tao")
TAOSTATS_API_KEY = os.getenv("TAOSTATS_API_KEY", "")
TAOSTATS_TIMEOUT = float(os.getenv("TAOSTATS_TIMEOUT", "10.0"))

# CoinGecko configuration
USE_COINGECKO = os.getenv("USE_COINGECKO", "false").lower() == "true"
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_TAO_ID = "bittensor"  # CoinGecko's id for TAO

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


async def fetch_tao_price_from_coingecko() -> TaoPriceData:
    """
    Fetch TAO/USD price and market data from CoinGecko API.
    
    Returns:
        TaoPriceData with price and additional market data.
        
    Raises:
        Exception if fetch fails.
    """
    params = {
        "ids": COINGECKO_TAO_ID,
        "vs_currencies": "usd",
        "include_market_cap": "true",
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
    }
    if COINGECKO_API_KEY:
        params["x_cg_demo_api_key"] = COINGECKO_API_KEY
    
    async with httpx.AsyncClient(timeout=TAOSTATS_TIMEOUT) as client:
        response = await client.get(COINGECKO_API_URL, params=params)
        response.raise_for_status()
        data = response.json()
        
        # CoinGecko returns: {"bittensor": {"usd": 123.45, "usd_market_cap": ..., ...}}
        if COINGECKO_TAO_ID not in data:
            raise ValueError(f"Could not find {COINGECKO_TAO_ID} in CoinGecko response: {data}")
        
        tao_data = data[COINGECKO_TAO_ID]
        price = tao_data.get("usd")
        if price is None:
            raise ValueError(f"Could not find USD price in CoinGecko response: {data}")
        
        # Fetch additional data from the detailed endpoint for 7d/30d changes
        price_change_7d = None
        price_change_30d = None
        market_cap_change_24h = None
        
        try:
            detailed_url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_TAO_ID}"
            detailed_params = {
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            }
            if COINGECKO_API_KEY:
                detailed_params["x_cg_demo_api_key"] = COINGECKO_API_KEY
            
            detailed_response = await client.get(detailed_url, params=detailed_params)
            detailed_response.raise_for_status()
            detailed_data = detailed_response.json()
            
            market_data = detailed_data.get("market_data", {})
            price_change_7d = market_data.get("price_change_percentage_7d")
            price_change_30d = market_data.get("price_change_percentage_30d")
            market_cap_change_24h = market_data.get("market_cap_change_percentage_24h")
        except Exception as e:
            # Log but don't fail - the basic data is still valuable
            logger.warning(f"Failed to fetch detailed CoinGecko data: {e}")
        
        return TaoPriceData(
            price=float(price),
            market_cap=tao_data.get("usd_market_cap"),
            volume_24h=tao_data.get("usd_24h_vol"),
            price_change_24h=tao_data.get("usd_24h_change"),
            market_cap_change_24h=market_cap_change_24h,
            price_change_7d=price_change_7d,
            price_change_30d=price_change_30d,
            source="coingecko",
        )


async def fetch_tao_price_from_taostats() -> TaoPriceData:
    """
    Fetch TAO/USD price from TaoStats API.
    
    Returns:
        TaoPriceData with price only (TaoStats doesn't provide market data).
        
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
            raise ValueError(f"Could not find price in TaoStats response: {data}")
        
        return TaoPriceData(
            price=float(price),
            source="taostats",
        )


async def fetch_tao_price_from_api() -> TaoPriceData:
    """
    Fetch TAO/USD price from configured API source.
    
    Uses CoinGecko if USE_COINGECKO=true, otherwise uses TaoStats.
    
    Returns:
        TaoPriceData with price and optional market data.
        
    Raises:
        Exception if fetch fails.
    """
    if USE_COINGECKO:
        return await fetch_tao_price_from_coingecko()
    else:
        return await fetch_tao_price_from_taostats()


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
            price_data = await fetch_tao_price_from_api()
            timestamp = datetime.now(timezone.utc)
            
            # Build the data dict - always include price and date
            db_data = {
                "taoPrice": price_data.price,
                "date": timestamp,
                "source": price_data.source,
            }
            
            # Add optional fields if available (from CoinGecko)
            if price_data.market_cap is not None:
                db_data["marketCap"] = price_data.market_cap
            if price_data.volume_24h is not None:
                db_data["volume24h"] = price_data.volume_24h
            if price_data.price_change_24h is not None:
                db_data["priceChange24h"] = price_data.price_change_24h
            if price_data.market_cap_change_24h is not None:
                db_data["marketCapChange24h"] = price_data.market_cap_change_24h
            if price_data.price_change_7d is not None:
                db_data["priceChange7d"] = price_data.price_change_7d
            if price_data.price_change_30d is not None:
                db_data["priceChange30d"] = price_data.price_change_30d
            
            # Write to database
            await _prisma.taousdprice.create(data=db_data)
            
            logger.info(f"TAO price written to database: ${price_data.price:.2f} at {timestamp.isoformat()} (source: {price_data.source})")
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
        source = "CoinGecko" if USE_COINGECKO else "TaoStats"
        logger.info(f"TAO price history task started (interval: {TAO_PRICE_HISTORY_REFRESH_SECONDS}s, source: {source})")
    return _history_task


def stop_history_task() -> None:
    """Stop the background price history task. Call this at app shutdown."""
    global _history_task
    if _history_task and not _history_task.done():
        _history_task.cancel()
        logger.info("TAO price history task stopped")


