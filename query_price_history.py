#!/usr/bin/env python3
"""
Query script to check TAO price history data in the database.
"""

import asyncio
import os
from datetime import datetime, timedelta
from prisma import Prisma

async def query_price_history():
    """Query and display TAO price history from the database."""
    prisma = Prisma()
    
    try:
        await prisma.connect()
        print("Connected to database\n")
        
        # Get total count
        total_count = await prisma.taousdprice.count()
        print(f"Total price records: {total_count}\n")
        
        if total_count == 0:
            print("No price records found in database.")
            return
        
        # Get latest 10 records
        print("Latest 10 price records:")
        print("-" * 60)
        latest = await prisma.taousdprice.find_many(
            take=10,
            order={"date": "desc"}
        )
        
        for record in latest:
            print(f"ID: {record.id:6d} | Price: ${record.taoPrice:10.2f} | Date: {record.date}")
        
        print("\n" + "-" * 60)
        
        # Get oldest record
        oldest = await prisma.taousdprice.find_first(
            order={"date": "asc"}
        )
        if oldest:
            print(f"\nOldest record: ${oldest.taoPrice:.2f} at {oldest.date}")
        
        # Get newest record
        newest = await prisma.taousdprice.find_first(
            order={"date": "desc"}
        )
        if newest:
            print(f"Newest record: ${newest.taoPrice:.2f} at {newest.date}")
        
        # Get records from last 24 hours
        yesterday = datetime.now() - timedelta(days=1)
        recent_count = await prisma.taousdprice.count(
            where={"date": {"gte": yesterday}}
        )
        print(f"\nRecords in last 24 hours: {recent_count}")
        
        # Get average price
        all_records = await prisma.taousdprice.find_many()
        if all_records:
            avg_price = sum(r.taoPrice for r in all_records) / len(all_records)
            print(f"Average price: ${avg_price:.2f}")
        
    except Exception as e:
        print(f"Error querying database: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await prisma.disconnect()
        print("\nDisconnected from database")

if __name__ == "__main__":
    asyncio.run(query_price_history())


