"""
Background task scheduler for the Talisman AI API.

Runs periodic jobs:
- Narrative cache refresh (every 15 min)
- Event cluster merging (every 15 min)
- Narrative lifecycle updates (every 6 hours)
- Narrative centroid drift (every 6 hours)
- Narrative auto-discovery (daily)
- Narrative split/merge detection (daily)
- Narrative seed check (on startup)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("background_tasks")


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

    try:
        await seed_narratives(prisma)
    except Exception as e:
        logger.warning(f"Failed to seed narratives: {e}")

    try:
        await _narrative_cache.refresh(prisma)
        logger.info("Narrative match cache initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize narrative cache: {e}")

    iteration = 0
    while True:
        try:
            iteration += 1

            # Every 15 min: merge event clusters + refresh narrative cache
            if iteration % 1 == 0:
                try:
                    await merge_fragmented_clusters(prisma)
                except Exception as e:
                    logger.warning(f"Event cluster merge failed: {e}")

                try:
                    await _narrative_cache.refresh(prisma)
                except Exception as e:
                    logger.warning(f"Narrative cache refresh failed: {e}")

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
