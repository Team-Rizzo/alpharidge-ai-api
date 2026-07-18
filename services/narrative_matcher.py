"""
Narrative matching service for the Alpharidge AI API.

Three-signal hybrid matching:
  Signal 1: Slug exact match (confidence 0.95)
  Signal 2: Embedding cosine similarity (threshold 0.55)
  Signal 3: Keyword Jaccard overlap fallback (threshold 0.50)

Also handles:
  - Auto-discovery with semantic dedup and candidate clustering
  - Narrative lifecycle (emerging → active → peak → fading → dormant)
  - Centroid drift via exponential moving average
  - Split/merge detection for narrative evolution
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
from prisma import Json, Prisma

logger = logging.getLogger("narrative_matcher")

NARRATIVES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "..", "alpharidge-ai", "alpharidge_ai", "analyzer", "data", "narratives.json",
)

EMBEDDING_DIM = 384
SLUG_MATCH_CONFIDENCE = 0.95
EMBEDDING_MATCH_THRESHOLD = 0.55
KEYWORD_MATCH_THRESHOLD = 0.50

AUTO_DISCOVERY_ARTICLE_THRESHOLD = 10
AUTO_DISCOVERY_SOURCE_THRESHOLD = 3
AUTO_DISCOVERY_WINDOW_DAYS = 7
MAX_ACTIVE_NARRATIVES = 200

CENTROID_ALPHA = 0.05
SPLIT_VARIANCE_THRESHOLD = 0.50
MERGE_COSINE_THRESHOLD = 0.85
MERGE_ARTICLE_OVERLAP = 0.30
CANDIDATE_DEDUP_THRESHOLD = 0.75
CANDIDATE_CLUSTER_THRESHOLD = 0.70


def _normalize_keyword(kw: str) -> str:
    return kw.lower().replace("_", " ").replace("-", " ").strip()


def _keyword_similarity(kw_a: str, kw_b: str) -> float:
    a = _normalize_keyword(kw_a)
    b = _normalize_keyword(kw_b)
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


# ============================================================================
# NarrativeMatchCache — in-memory narrative embeddings for fast matching
# ============================================================================

class NarrativeMatchCache:
    """Holds active narrative data in memory for sub-millisecond matching."""

    def __init__(self):
        self._ids: List[int] = []
        self._slugs: List[str] = []
        self._names: List[str] = []
        self._keyword_lists: List[List[str]] = []
        self._matrix: Optional[np.ndarray] = None

    @property
    def loaded(self) -> bool:
        return self._matrix is not None and len(self._ids) > 0

    async def refresh(self, db: Prisma):
        narratives = await db.narrative.find_many(
            where={"phase": {"in": ["active", "emerging", "peak"]}},
        )
        ids = []
        slugs = []
        names = []
        keyword_lists = []
        embeddings = []

        for n in narratives:
            ids.append(n.id)
            slugs.append(n.slug.replace("-", " "))
            names.append(_normalize_keyword(n.name))
            kws = n.keywords if isinstance(n.keywords, list) else []
            keyword_lists.append([_normalize_keyword(k) for k in kws])
            emb = n.embedding if n.embedding and len(n.embedding) == EMBEDDING_DIM else None
            embeddings.append(emb)

        self._ids = ids
        self._slugs = slugs
        self._names = names
        self._keyword_lists = keyword_lists

        if any(e is not None for e in embeddings):
            rows = [np.array(e, dtype=np.float32) if e else np.zeros(EMBEDDING_DIM, dtype=np.float32)
                    for e in embeddings]
            self._matrix = np.stack(rows)
        else:
            self._matrix = None

        logger.info(f"[CACHE] Refreshed: {len(ids)} narratives, "
                    f"{sum(1 for e in embeddings if e)} with embeddings")

    def match(self, narrative_embedding: Optional[List[float]],
              keywords: List[str]) -> List[Tuple[int, float, str]]:
        """Match against cached narratives. Returns [(narrative_id, confidence, match_type)]."""
        if not self._ids:
            return []

        scores: Dict[int, Tuple[float, str]] = {}

        for kw in keywords:
            kw_norm = _normalize_keyword(kw)

            # Signal 1: Slug exact match
            for i, slug_norm in enumerate(self._slugs):
                if kw_norm == slug_norm:
                    nid = self._ids[i]
                    if nid not in scores or scores[nid][0] < SLUG_MATCH_CONFIDENCE:
                        scores[nid] = (SLUG_MATCH_CONFIDENCE, "slug")

            # Signal 3: Keyword Jaccard + substring + name matching
            for i, kw_list in enumerate(self._keyword_lists):
                nid = self._ids[i]
                existing = scores.get(nid, (0.0, ""))

                for nk in kw_list:
                    sim = _keyword_similarity(kw_norm, nk)
                    if sim > existing[0] and sim >= KEYWORD_MATCH_THRESHOLD:
                        scores[nid] = (sim, "keyword")
                        existing = scores[nid]

                for nk in kw_list:
                    nk_norm = _normalize_keyword(nk)
                    if (kw_norm in nk_norm or nk_norm in kw_norm) and existing[0] < 0.65:
                        scores[nid] = (0.65, "substring")
                        existing = scores[nid]

                name_sim = _keyword_similarity(kw_norm, self._names[i])
                if name_sim > existing[0] and name_sim >= KEYWORD_MATCH_THRESHOLD:
                    scores[nid] = (name_sim, "name")
                    existing = scores[nid]
                if (kw_norm in self._names[i] or self._names[i] in kw_norm) and existing[0] < 0.65:
                    scores[nid] = (0.65, "name_substring")

        # Signal 2: Embedding cosine similarity
        if narrative_embedding and self._matrix is not None:
            query = np.array(narrative_embedding, dtype=np.float32)
            norm = np.linalg.norm(query)
            if norm > 0.01:
                query = query / norm
                sims = self._matrix @ query
                for i, sim in enumerate(sims):
                    if sim >= EMBEDDING_MATCH_THRESHOLD:
                        nid = self._ids[i]
                        existing = scores.get(nid, (0.0, ""))
                        if float(sim) > existing[0]:
                            scores[nid] = (float(sim), "embedding")

        results = [(nid, conf, mtype) for nid, (conf, mtype) in scores.items()
                    if conf >= KEYWORD_MATCH_THRESHOLD]
        results.sort(key=lambda x: -x[1])
        return results


_narrative_cache = NarrativeMatchCache()


# ============================================================================
# Seeding
# ============================================================================

async def seed_narratives(db: Prisma):
    count = await db.narrative.count()
    if count > 0:
        logger.info(f"Narrative table already has {count} entries, skipping seed")
        return

    narratives_path = NARRATIVES_FILE
    alt_path = os.path.join(os.path.dirname(__file__), "..", "data", "narratives.json")
    for p in [narratives_path, alt_path]:
        if os.path.exists(p):
            narratives_path = p
            break

    if not os.path.exists(narratives_path):
        logger.warning(f"narratives.json not found at {narratives_path}")
        return

    with open(narratives_path) as f:
        seed_data = json.load(f)

    for n in seed_data:
        try:
            await db.narrative.create(
                data={
                    "slug": n["slug"],
                    "name": n["name"],
                    "description": n.get("description"),
                    "keywords": Json(n.get("keywords", [])),
                    "sectorIds": Json(n.get("sector_ids")) if n.get("sector_ids") is not None else None,
                    "phase": n.get("status", "active"),
                    "source": "seed",
                },
            )
        except Exception as e:
            logger.debug(f"Skipping narrative {n['slug']}: {e}")

    logger.info(f"Seeded {len(seed_data)} narratives from {narratives_path}")


# ============================================================================
# Matching
# ============================================================================

async def match_article_narratives(
    db: Prisma,
    article_id: int,
    narrative_keywords: List[str],
    sector_id: Optional[int] = None,
    source: Optional[str] = None,
    narrative_embedding: Optional[List[float]] = None,
) -> List[int]:
    """Match article against narratives using hybrid 3-signal approach."""
    if not narrative_keywords:
        return []

    if not _narrative_cache.loaded:
        await _narrative_cache.refresh(db)

    matches = _narrative_cache.match(narrative_embedding, narrative_keywords)
    matched_ids = []

    for narr_id, confidence, match_type in matches:
        try:
            kw_str = ", ".join(narrative_keywords)[:200]
            await db.narrativearticle.create(
                data={
                    "narrativeId": narr_id,
                    "articleId": article_id,
                    "confidence": min(1.0, confidence),
                    "matchedKeyword": f"[{match_type}] {kw_str}",
                },
            )
            matched_ids.append(narr_id)

            await db.narrative.update(
                where={"id": narr_id},
                data={
                    "articleCount": {"increment": 1},
                    "lastArticleAt": datetime.now(timezone.utc),
                },
            )
        except Exception:
            pass

    unmatched = [kw for kw in narrative_keywords
                 if not any(_normalize_keyword(kw) == _narrative_cache._slugs[
                     _narrative_cache._ids.index(nid)] if nid in _narrative_cache._ids else False
                     for nid in matched_ids)]

    if not matched_ids:
        for kw in narrative_keywords:
            kw_lower = kw.lower().strip()
            if kw_lower:
                await _track_candidate(db, kw_lower, source)

    return matched_ids


async def _track_candidate(db: Prisma, keyword: str, source: Optional[str]):
    existing = await db.narrativecandidate.find_first(where={"keyword": keyword})

    if existing:
        await db.narrativecandidate.update(
            where={"id": existing.id},
            data={
                "articleCount": {"increment": 1},
                "lastSeenAt": datetime.now(timezone.utc),
            },
        )
    else:
        try:
            await db.narrativecandidate.create(
                data={"keyword": keyword, "articleCount": 1, "sourceCount": 1},
            )
        except Exception:
            pass


# ============================================================================
# Auto-Discovery (with semantic dedup + candidate clustering)
# ============================================================================

async def auto_discover_narratives(db: Prisma):
    """Promote candidates with semantic dedup — prevents near-duplicate narratives."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_DISCOVERY_WINDOW_DAYS)
    candidates = await db.narrativecandidate.find_many(
        where={
            "articleCount": {"gte": AUTO_DISCOVERY_ARTICLE_THRESHOLD},
            "lastSeenAt": {"gte": cutoff},
            "promoted": False,
        },
    )

    if not candidates:
        return

    active_count = await db.narrative.count(
        where={"phase": {"in": ["active", "emerging", "peak"]}},
    )

    # Load active narrative embeddings for dedup check
    active_narratives = await db.narrative.find_many(
        where={"phase": {"in": ["active", "emerging", "peak"]}},
    )
    active_embeddings = []
    for n in active_narratives:
        if n.embedding and len(n.embedding) == EMBEDDING_DIM:
            active_embeddings.append(np.array(n.embedding, dtype=np.float32))
    active_matrix = np.stack(active_embeddings) if active_embeddings else None

    # Cluster similar candidates before promotion
    cand_embeddings = {}
    for c in candidates:
        if c.embedding and len(c.embedding) == EMBEDDING_DIM:
            cand_embeddings[c.id] = np.array(c.embedding, dtype=np.float32)

    # Group similar candidates
    merged_into: Dict[int, int] = {}
    cand_ids = list(cand_embeddings.keys())
    for i, cid_a in enumerate(cand_ids):
        if cid_a in merged_into:
            continue
        for cid_b in cand_ids[i + 1:]:
            if cid_b in merged_into:
                continue
            sim = float(cand_embeddings[cid_a] @ cand_embeddings[cid_b])
            if sim >= CANDIDATE_CLUSTER_THRESHOLD:
                # Merge B into A (A has lower index = processed first)
                merged_into[cid_b] = cid_a

    promoted = 0
    for cand in candidates:
        if active_count + promoted >= MAX_ACTIVE_NARRATIVES:
            break
        if cand.id in merged_into:
            continue

        # Semantic dedup: check if candidate is too close to existing narrative
        if cand.id in cand_embeddings and active_matrix is not None:
            sims = active_matrix @ cand_embeddings[cand.id]
            if np.max(sims) >= CANDIDATE_DEDUP_THRESHOLD:
                best_idx = int(np.argmax(sims))
                logger.info(f"Candidate '{cand.keyword}' too similar to narrative "
                            f"'{active_narratives[best_idx].name}' ({sims[best_idx]:.3f}), skipping")
                await db.narrativecandidate.update(
                    where={"id": cand.id}, data={"promoted": True},
                )
                continue

        slug = cand.keyword.lower().replace(" ", "-").replace("/", "-")[:50]
        existing = await db.narrative.find_first(where={"slug": slug})
        if existing:
            continue

        emb = list(cand_embeddings[cand.id]) if cand.id in cand_embeddings else []

        try:
            await db.narrative.create(
                data={
                    "slug": slug,
                    "name": cand.keyword.title(),
                    "description": f"Auto-discovered narrative: {cand.keyword}",
                    "keywords": Json([cand.keyword]),
                    "phase": "emerging",
                    "source": "discovered",
                    "articleCount": cand.articleCount,
                    "embedding": emb,
                },
            )
            await db.narrativecandidate.update(
                where={"id": cand.id}, data={"promoted": True},
            )
            promoted += 1
            logger.info(f"Auto-discovered narrative: {cand.keyword} ({cand.articleCount} articles)")
        except Exception as e:
            logger.warning(f"Failed to promote candidate {cand.keyword}: {e}")

    if promoted:
        logger.info(f"Promoted {promoted} narrative candidates")
        await _narrative_cache.refresh(db)


# ============================================================================
# Lifecycle
# ============================================================================

async def update_narrative_lifecycle(db: Prisma):
    now = datetime.now(timezone.utc)

    await db.narrative.update_many(
        where={"phase": "emerging", "articleCount": {"gte": 20}},
        data={"phase": "active"},
    )

    fading_cutoff = now - timedelta(days=5)
    await db.narrative.update_many(
        where={
            "phase": {"in": ["active", "peak"]},
            "lastArticleAt": {"lt": fading_cutoff},
        },
        data={"phase": "fading"},
    )

    dormant_cutoff = now - timedelta(days=30)
    await db.narrative.update_many(
        where={
            "phase": "fading",
            "lastArticleAt": {"lt": dormant_cutoff},
        },
        data={"phase": "dormant"},
    )

    await db.narrative.update_many(
        where={
            "phase": "dormant",
            "lastArticleAt": {"gte": fading_cutoff},
        },
        data={"phase": "active"},
    )

    await _narrative_cache.refresh(db)


# ============================================================================
# Centroid Drift (EMA)
# ============================================================================

async def update_narrative_centroids(db: Prisma):
    """Update narrative embeddings via exponential moving average of recent articles."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    active_narratives = await db.narrative.find_many(
        where={"phase": {"in": ["active", "emerging", "peak"]}},
    )

    updated = 0
    for narr in active_narratives:
        if not narr.embedding or len(narr.embedding) != EMBEDDING_DIM:
            continue

        recent_articles = await db.narrativearticle.find_many(
            where={
                "narrativeId": narr.id,
                "addedAt": {"gte": cutoff},
            },
            take=100,
        )

        if len(recent_articles) < 5:
            continue

        article_ids = [na.articleId for na in recent_articles]
        analyses = await db.newsarticleanalysis.find_many(
            where={"articleId": {"in": article_ids}},
        )

        article_embeddings = []
        for a in analyses:
            if a.narrativeEmbedding and len(a.narrativeEmbedding) == EMBEDDING_DIM:
                article_embeddings.append(np.array(a.narrativeEmbedding, dtype=np.float32))

        if len(article_embeddings) < 3:
            continue

        current = np.array(narr.embedding, dtype=np.float32)
        article_mean = np.mean(article_embeddings, axis=0)
        new_centroid = CENTROID_ALPHA * article_mean + (1 - CENTROID_ALPHA) * current
        norm = np.linalg.norm(new_centroid)
        if norm > 0:
            new_centroid = new_centroid / norm

        await db.narrative.update(
            where={"id": narr.id},
            data={"embedding": new_centroid.tolist()},
        )
        updated += 1

    if updated:
        logger.info(f"Updated {updated} narrative centroids via EMA")
        await _narrative_cache.refresh(db)


# ============================================================================
# Split/Merge Detection
# ============================================================================

async def detect_narrative_splits(db: Prisma):
    """Detect narratives whose articles are becoming bimodal in embedding space."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    active_narratives = await db.narrative.find_many(
        where={"phase": {"in": ["active", "peak"]}},
    )

    for narr in active_narratives:
        recent_links = await db.narrativearticle.find_many(
            where={"narrativeId": narr.id, "addedAt": {"gte": cutoff}},
            take=200,
        )
        if len(recent_links) < 10:
            continue

        article_ids = [na.articleId for na in recent_links]
        analyses = await db.newsarticleanalysis.find_many(
            where={"articleId": {"in": article_ids}},
        )

        embeddings = []
        for a in analyses:
            if a.narrativeEmbedding and len(a.narrativeEmbedding) == EMBEDDING_DIM:
                embeddings.append(np.array(a.narrativeEmbedding, dtype=np.float32))

        if len(embeddings) < 10:
            continue

        matrix = np.stack(embeddings)
        sims = matrix @ matrix.T
        n = len(sims)
        mask = np.ones((n, n), dtype=bool)
        np.fill_diagonal(mask, False)
        mean_sim = sims[mask].mean()

        if mean_sim < SPLIT_VARIANCE_THRESHOLD:
            logger.warning(f"Narrative '{narr.name}' may be splitting: "
                           f"mean internal similarity {mean_sim:.3f} < {SPLIT_VARIANCE_THRESHOLD}")


async def detect_narrative_merges(db: Prisma):
    """Detect narrative pairs that are converging in embedding space."""
    active_narratives = await db.narrative.find_many(
        where={"phase": {"in": ["active", "emerging", "peak"]}},
    )

    embeddings = []
    valid_narratives = []
    for n in active_narratives:
        if n.embedding and len(n.embedding) == EMBEDDING_DIM:
            embeddings.append(np.array(n.embedding, dtype=np.float32))
            valid_narratives.append(n)

    if len(embeddings) < 2:
        return

    matrix = np.stack(embeddings)
    sims = matrix @ matrix.T

    for i in range(len(valid_narratives)):
        for j in range(i + 1, len(valid_narratives)):
            if sims[i][j] >= MERGE_COSINE_THRESHOLD:
                logger.warning(
                    f"Narratives may be merging: '{valid_narratives[i].name}' and "
                    f"'{valid_narratives[j].name}' (cosine {sims[i][j]:.3f})")
