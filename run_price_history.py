#!/usr/bin/env python3
"""
Standalone script to run the TAO price history service.
This can be run as a separate PM2 process.

Supports PRICE_DATABASE_URL for writing to a separate database.
"""

import os

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path)
except ImportError:
    pass

import asyncio
from prisma import Prisma
from services.tao_price_history import (
    connect_price_database,
    disconnect_price_database,
    set_prisma_client,
    start_history_task,
    PRICE_DATABASE_URL,
)

async def main():
    """Initialize and run the price history service."""
    prisma = None
    
    try:
        # Connect to database - either PRICE_DATABASE_URL or default DATABASE_URL
        if PRICE_DATABASE_URL:
            await connect_price_database()
            print(f"Using PRICE_DATABASE_URL for price data")
        else:
            prisma = Prisma()
            await prisma.connect()
            print("Connected to default database")
            set_prisma_client(prisma)
        
        # Start the history task
        print("Starting TAO price history service...")
        task = start_history_task()
        
        # Keep running
        print("Service running. Press Ctrl+C to stop.")
        await task
        
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Disconnect from the appropriate database
        if PRICE_DATABASE_URL:
            await disconnect_price_database()
        elif prisma:
            await prisma.disconnect()
        print("Disconnected from database")

if __name__ == "__main__":
    asyncio.run(main())


