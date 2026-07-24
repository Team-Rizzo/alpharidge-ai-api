"""
Event clustering service for the Alpharidge AI API.

Multi-phase clustering:
1. Fingerprint match (exact)
2. Content hash dedup
3. Embedding cosine similarity (threshold 0.75)
4. Title word-overlap fallback (Jaccard > 0.6)
5. Create new event

Background: merge fragmented clusters using title similarity + embedding cosine.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import numpy as np
from prisma import Json, Prisma

logger = logging.getLogger("event_clustering")

EMBEDDING_DIM = 384
EMBEDDING_MATCH_THRESHOLD = 0.75
EMBEDDING_MERGE_THRESHOLD = 0.80
TITLE_MATCH_THRESHOLD = 0.6


def compute_event_fingerprint(event_type: str, event_date: Optional[str], entities: list) -> str:
    sorted_entities = sorted([str(e).lower().strip() for e in entities[:3]])
    raw = f"{event_type}|{event_date or 'none'}|{'|'.join(sorted_entities)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cosine_sim(a: List[float], b: List[float]) -> float:
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(va @ vb / (na * nb))


async def cluster_article(
    db: Prisma,
    article_id: int,
    analysis_data: Optional[dict],
) -> Optional[int]:
    if not analysis_data:
        return None

    # Triage-only submissions (schema v3: articles a miner filtered out as not
    # market-relevant) carry a triage claim and proof-of-read but no analytic
    # payload. Clustering them would compute the same empty fingerprint for
    # every one and collapse the entire filtered stream into a single event.
    if not analysis_data.get("event_fingerprint"):
        return None

    event_fp = analysis_data.get("event_fingerprint", {})
    event_type = event_fp.get("event_type", "other")
    event_title = event_fp.get("event_title", "")
    event_date_str = event_fp.get("event_date")
    content_hash = event_fp.get("content_hash", "")

    entities = [e.get("name", "") for e in analysis_data.get("entities", [])[:5]]
    sentiment = analysis_data.get("overall_sentiment", "neutral")
    impact = analysis_data.get("impact_potential", "low")
    sector_id = analysis_data.get("topic_signature", {}).get("primary_sector_id")
    sector_symbol = analysis_data.get("topic_signature", {}).get("primary_sector_symbol")

    title_emb = analysis_data.get("title_embedding")
    if title_emb and isinstance(title_emb, list) and len(title_emb) == EMBEDDING_DIM:
        pass
    else:
        title_emb = None

    fingerprint = compute_event_fingerprint(event_type, event_date_str, entities)

    # Phase 1: Exact fingerprint match
    existing = await db.event.find_first(where={"fingerprint": fingerprint})
    if existing:
        await _attach_article(db, existing.id, article_id, sentiment, entities)
        return existing.id

    # Phase 2: Content hash dedup
    if content_hash:
        dup_analysis = await db.newsarticleanalysis.find_first(
            where={"contentHash": content_hash, "articleId": {"not": article_id}},
        )
        if dup_analysis:
            dup_event = await db.eventarticle.find_first(
                where={"articleId": dup_analysis.articleId},
            )
            if dup_event:
                await _attach_article(db, dup_event.eventId, article_id, sentiment, entities)
                return dup_event.eventId

    # Phase 3: Embedding similarity (same event_type + time window)
    if title_emb:
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        candidates = await db.event.find_many(
            where={
                "eventType": event_type,
                "lastArticleAt": {"gte": cutoff},
            },
            take=20,
        )
        for cand in candidates:
            if cand.titleEmbedding and len(cand.titleEmbedding) == EMBEDDING_DIM:
                sim = _cosine_sim(title_emb, cand.titleEmbedding)
                if sim >= EMBEDDING_MATCH_THRESHOLD:
                    await _attach_article(db, cand.id, article_id, sentiment, entities)
                    return cand.id

    # Phase 4: Title word-overlap fallback
    if event_title and len(event_title) > 10:
        title_words = event_title.lower().split()[:4]
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)

        for word in title_words:
            if len(word) < 4:
                continue
            candidates = await db.event.find_many(
                where={
                    "eventType": event_type,
                    "lastArticleAt": {"gte": cutoff},
                    "canonicalTitle": {"contains": word, "mode": "insensitive"},
                },
                take=5,
            )
            for cand in candidates:
                sim = _title_similarity(event_title.lower(), cand.canonicalTitle.lower())
                if sim > TITLE_MATCH_THRESHOLD:
                    await _attach_article(db, cand.id, article_id, sentiment, entities)
                    return cand.id

    # Phase 5: Create new event
    event_date = None
    if event_date_str:
        try:
            # Keep the datetime: the column is DateTime? @db.Date, and the client cannot
            # serialise a bare date (every create raised "date not serializable").
            event_date = datetime.strptime(event_date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    new_event = await db.event.create(
        data={
            "fingerprint": fingerprint,
            "eventType": event_type,
            "canonicalTitle": event_title or "Untitled Event",
            "eventDate": event_date,
            "sectorId": sector_id,
            "sectorSymbol": sector_symbol,
            "articleCount": 1,
            "sentiment": sentiment,
            "impactPotential": impact,
            "entities": Json(entities),  # column is Json? @db.JsonB; a bare list is rejected
            "titleEmbedding": title_emb or [],
        },
    )
    await db.eventarticle.create(
        data={"eventId": new_event.id, "articleId": article_id, "role": "reporting"},
    )
    logger.info(f"Created new event {new_event.id}: {event_title}")
    return new_event.id


async def _attach_article(
    db: Prisma,
    event_id: int,
    article_id: int,
    sentiment: str,
    entities: list,
):
    try:
        await db.eventarticle.create(
            data={"eventId": event_id, "articleId": article_id, "role": "reporting"},
        )
    except Exception:
        return

    await db.event.update(
        where={"id": event_id},
        data={
            "articleCount": {"increment": 1},
            "lastArticleAt": datetime.now(timezone.utc),
        },
    )


def _title_similarity(a: str, b: str) -> float:
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


async def merge_fragmented_clusters(db: Prisma):
    """Merge event clusters using title similarity + embedding cosine."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent_events = await db.event.find_many(
        where={"lastArticleAt": {"gte": cutoff}, "articleCount": {"gt": 0}},
        order={"articleCount": "desc"},
    )

    merged_count = 0
    merged_ids = set()

    for i, event_a in enumerate(recent_events):
        if event_a.id in merged_ids:
            continue
        for event_b in recent_events[i + 1:]:
            if event_b.id in merged_ids:
                continue
            if event_a.eventType != event_b.eventType:
                continue

            should_merge = False

            # Check title similarity
            title_sim = _title_similarity(
                event_a.canonicalTitle.lower(),
                event_b.canonicalTitle.lower(),
            )
            if title_sim >= TITLE_MATCH_THRESHOLD:
                should_merge = True

            # Check embedding similarity
            if (not should_merge
                    and event_a.titleEmbedding and len(event_a.titleEmbedding) == EMBEDDING_DIM
                    and event_b.titleEmbedding and len(event_b.titleEmbedding) == EMBEDDING_DIM):
                emb_sim = _cosine_sim(event_a.titleEmbedding, event_b.titleEmbedding)
                if emb_sim >= EMBEDDING_MERGE_THRESHOLD:
                    should_merge = True

            if not should_merge:
                continue

            articles_b = await db.eventarticle.find_many(where={"eventId": event_b.id})
            for ea in articles_b:
                try:
                    await db.eventarticle.update(
                        where={"id": ea.id},
                        data={"eventId": event_a.id},
                    )
                except Exception:
                    pass

            await db.event.update(
                where={"id": event_a.id},
                data={"articleCount": {"increment": event_b.articleCount}},
            )
            await db.event.delete(where={"id": event_b.id})
            merged_ids.add(event_b.id)
            merged_count += 1

    if merged_count:
        logger.info(f"Merged {merged_count} fragmented event clusters")
