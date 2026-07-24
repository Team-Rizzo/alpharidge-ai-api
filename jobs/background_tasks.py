"""
Background task scheduler for the Alpharidge AI API.

Runs periodic jobs:
- Narrative cache refresh (every 15 min)
- Miner leaderboard snapshot refresh (every 15 min)
- Narrative seed check (on startup)

Gated behind BACKGROUND_JOBS_FULL=true (default OFF, see below):
- Event cluster merging (every 15 min)
- Narrative lifecycle updates (every 6 hours)
- Narrative centroid drift (every 6 hours)
- Narrative auto-discovery (daily)
- Narrative split/merge detection (daily)

The gate exists because this loop was dead in production for months (a
missing `asyncio` import in main.py meant it never started) and the corpus
grew far past what the gated jobs were written for: merge_fragmented_clusters
loads every event from the last 48h (~293K rows incl. embeddings) and does
O(n^2) pairwise similarity in pure Python on the event loop — enabling it
now would stall every request for hours. Bound those jobs before flipping
the flag.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("background_tasks")

BACKGROUND_JOBS_FULL = os.getenv("BACKGROUND_JOBS_FULL", "false").lower() == "true"


async def run_periodic_jobs(prisma):
    """Main loop for background jobs. Called once at startup."""
    from services.narrative_matcher import (
        _narrative_cache,
        seed_narratives,
        auto_discover_narratives,
        update_narrative_lifecycle,
        update_narrative_centroids,
        detect_narrative_splits,
        detect_narrative_merges,
    )
    from services.event_clustering import merge_fragmented_clusters
    from dashboard_routes import refresh_miners_snapshot

    try:
        await seed_narratives(prisma)
    except Exception as e:
        logger.warning(f"Failed to seed narratives: {e}")

    try:
        await _narrative_cache.refresh(prisma)
        logger.info("Narrative match cache initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize narrative cache: {e}")

    # Warm the miner leaderboard snapshot so the dashboard never serves an
    # empty /dashboard/miners while waiting for the first request-triggered
    # refresh (the aggregation takes ~45s).
    try:
        await refresh_miners_snapshot(prisma)
        logger.info("Miner leaderboard snapshot initialized")
    except Exception as e:
        logger.warning(f"Miner leaderboard warm failed: {e!r}")

    iteration = 0
    while True:
        try:
            iteration += 1

            # Every 15 min: refresh narrative cache + re-warm the miner
            # leaderboard snapshot
            try:
                await _narrative_cache.refresh(prisma)
            except Exception as e:
                logger.warning(f"Narrative cache refresh failed: {e}")

            try:
                await refresh_miners_snapshot(prisma)
            except Exception as e:
                logger.warning(f"Miner leaderboard refresh failed: {e!r}")

            if BACKGROUND_JOBS_FULL:
                # Every 15 min: merge event clusters
                try:
                    await merge_fragmented_clusters(prisma)
                except Exception as e:
                    logger.warning(f"Event cluster merge failed: {e}")

                # Every 6 hours: narrative lifecycle + centroid drift
                if iteration % 24 == 0:
                    try:
                        await update_narrative_lifecycle(prisma)
                        logger.info("Narrative lifecycle update completed")
                    except Exception as e:
                        logger.warning(f"Narrative lifecycle update failed: {e}")

                    try:
                        await update_narrative_centroids(prisma)
                        logger.info("Narrative centroid drift completed")
                    except Exception as e:
                        logger.warning(f"Narrative centroid drift failed: {e}")

                # Every 24 hours: auto-discover + split/merge detection
                if iteration % 96 == 0:
                    try:
                        await auto_discover_narratives(prisma)
                        logger.info("Narrative auto-discovery completed")
                    except Exception as e:
                        logger.warning(f"Narrative auto-discovery failed: {e}")

                    try:
                        await detect_narrative_splits(prisma)
                        await detect_narrative_merges(prisma)
                        logger.info("Narrative split/merge detection completed")
                    except Exception as e:
                        logger.warning(f"Narrative split/merge detection failed: {e}")

        except Exception as e:
            logger.error(f"Background task iteration {iteration} failed: {e}")

        await asyncio.sleep(900)
