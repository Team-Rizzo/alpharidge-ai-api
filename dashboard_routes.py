"""
Dashboard API routes — read-only endpoints restricted to local / allowed IPs.

Mounted on the main FastAPI app via `app.include_router(router)`.
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from dashboard_models import (
    SourceCounts,
    SentimentDistribution,
    DashboardStatsResponse,
    FeedItem,
    FeedItemAuthor,
    FeedItemArticleMeta,
    FeedItemTelegramMeta,
    FeedResponse,
    ArticleWithAnalysis,
    ArticlesResponse,
    ArticleDetailResponse,
    SourceStats,
    ArticleSourcesResponse,
    DailySentiment,
    SentimentResponse,
    TweetDetailResponse,
    TelegramDetailResponse,
    EventSummary,
    EventsResponse,
    EventDetailResponse,
    NarrativeSummary,
    NarrativesResponse,
    MinerLeaderboardEntry,
    MinerLeaderboardResponse,
    MinerRecentItem,
    MinerRewardEntry,
    MinerPenaltyEntry,
    MinerProfileResponse,
    Diagnosis,
    MinerBatch,
    MinerBatchesResponse,
    MinerBatchItem,
    MinerBatchItemsResponse,
    ReputationPoint,
    MinerReputationResponse,
    MinerEventRow,
    MinerEventsResponse,
    EarnedItem,
    ItemAnalysis,
    ItemEntity,
    AssetEntry,
    AssetCoverageResponse,
    ValidatorEntry,
    ValidatorActivityResponse,
)

logger = logging.getLogger(__name__)

# Peers allowed to reach internal-only routes. Localhost is always allowed; other
# internal hosts come from env (the reverse-proxy address is deliberately not listed,
# so proxied public requests can't reach these routes). Fail-closed: if the env is
# unset, only localhost is allowed.
_INTERNAL_ALLOWED_IPS: set[str] = {"127.0.0.1", "::1"}
_INTERNAL_ALLOWED_IPS.update(
    ip.strip()
    for ip in os.getenv("INTERNAL_ALLOWED_IPS", "").split(",")
    if ip.strip()
)

# Dashboard read routes are public; other guarded routes are internal-only.
_INTERNAL_DASHBOARD_PATHS: set[str] = {"/dashboard/miner-dispatch"}


def _is_public_dashboard(path: str) -> bool:
    return path.startswith("/dashboard/") and path not in _INTERNAL_DASHBOARD_PATHS


async def _require_local(request: Request):
    if _is_public_dashboard(request.url.path):
        return
    client_ip = request.client.host if request.client else None
    if client_ip not in _INTERNAL_ALLOWED_IPS:
        raise HTTPException(status_code=403, detail="Access restricted.")


router = APIRouter(prefix="/dashboard", dependencies=[Depends(_require_local)])


def _get_prisma():
    from main import prisma
    return prisma


# Epoch <-> block-window mapping (A5). Must match the validator's formula
# (validation_client.py: start = epoch*BLOCK_LENGTH, stop = (epoch+1)*BLOCK_LENGTH-1).
# Env-overridable; default 100 mirrors the validator's config default. Do not hardcode.
BLOCK_LENGTH = int(os.getenv("BLOCK_LENGTH", "100"))


def _block_window(epoch: int) -> tuple[int, int]:
    return epoch * BLOCK_LENGTH, (epoch + 1) * BLOCK_LENGTH - 1


def _as_json(val):
    """Normalize a JSONB column from query_raw: it may arrive already-parsed (dict/list)
    or as a JSON string depending on the driver. Returns None on anything unparseable."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return val


# Max entities surfaced per item on the drill-down. The full extraction can list dozens;
# the report-card only needs the salient few so the panel stays readable.
ITEM_ANALYSIS_MAX_ENTITIES = 12


def _build_item_analysis(row: dict) -> "ItemAnalysis":
    """Map a news_article_analysis row (+ its analysis_data JSON) to the display-only
    ItemAnalysis. Tolerant of missing/legacy rows: every field is optional, and the rich
    analysis_data blob (entities/assets/source/urgency) may be absent on older analyses."""
    data = _as_json(row.get("analysis_data")) or {}

    entities = []
    for e in (data.get("entities") or [])[:ITEM_ANALYSIS_MAX_ENTITIES]:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if not name:
            continue
        entities.append(ItemEntity(
            name=str(name),
            entity_type=e.get("entity_type"),
            role=e.get("role"),
            ticker=e.get("ticker"),
            mention_count=e.get("mention_count"),
        ))

    # `assets` can be a list of strings or of {symbol/ticker/name} dicts; normalize to symbols.
    assets = []
    for a in (data.get("assets") or []):
        if isinstance(a, str):
            assets.append(a)
        elif isinstance(a, dict):
            sym = a.get("symbol") or a.get("ticker") or a.get("name")
            if sym:
                assets.append(str(sym))

    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    event_date = row.get("event_date")

    return ItemAnalysis(
        impact_level=row.get("impact_level"),
        event_type=row.get("event_type"),
        event_date=event_date.isoformat() if hasattr(event_date, "isoformat") else (event_date or None),
        primary_geo=row.get("primary_geo"),
        factual_confidence=row.get("factual_confidence"),
        overall_sentiment_score=row.get("overall_sentiment_score"),
        urgency=data.get("urgency"),
        source_name=source.get("source_name") or source.get("source_id"),
        source_credibility=source.get("credibility_score"),
        is_original_reporting=source.get("is_original_reporting"),
        entities=entities,
        assets=assets,
        quote_count=len(data.get("quotes") or []),
        analyzed_by=row.get("miner_hotkey"),
    )


def _diagnose_batch(earned_items: int, penalty_items: int, breakdown: dict,
                    was_zeroed: bool) -> Diagnosis:
    """A6 diagnosis engine -- turn a batch's numbers into one human sentence.
    Server-side so the chip text is identical across all surfaces.

    The "zeroed your reward" headline gates on was_zeroed (derived from the authoritative
    post-zeroing rewards table), NOT on penalty_detail's validator count -- penalty_detail
    only captures new-build emitters and undercounts the true consensus flag count, so a
    miner zeroed by other validators may have few or zero detail rows here."""
    timeouts = breakdown.get("timeout", 0)
    mismatches = breakdown.get("classification_mismatch", 0)
    outdated = breakdown.get("missing_classification", 0) + breakdown.get("needs_update", 0)

    # On-chain zeroing is the consequence the miner cares about most -- lead with it,
    # even when this validator recorded no per-item detail for the epoch.
    if was_zeroed:
        if outdated > 0:
            action = "Pull the latest alpharidge-ai and restart."
        elif penalty_items > 0:
            action = "Fix the items in this batch."
        else:
            action = "Multiple validators disagreed with your work this epoch."
        return Diagnosis(severity="error", headline="This zeroed your on-chain reward.",
                         detail="You did valid work, but consensus zeroed your reward this epoch.",
                         action=action)

    if penalty_items == 0:
        if earned_items > 0:
            return Diagnosis(severity="ok", headline="Scoring cleanly.",
                             detail=f"{earned_items} item(s) matched the validator.", action=None)
        return Diagnosis(severity="ok", headline="No issues this batch.",
                         detail="Nothing was penalized.", action=None)

    # Outdated code is the most actionable of the non-zeroing cases -- surface it first.
    if outdated > 0:
        return Diagnosis(severity="error", headline="Your miner is on outdated code.",
                         detail="The validator couldn't read your classification.",
                         action="Pull the latest alpharidge-ai and restart.")

    if mismatches > 0 and timeouts == 0:
        return Diagnosis(severity="warn",
                         headline="Disagreeing with the validator's classification.",
                         detail=f"{mismatches} item(s) didn't match on at least one field.",
                         action="Review the field diffs below.")

    if timeouts > 0 and mismatches == 0:
        return Diagnosis(severity="warn", headline="Items timed out before scoring.",
                         detail=f"{timeouts} item(s) weren't returned in time.",
                         action="Check your response time / uptime.")

    # Mixed causes.
    return Diagnosis(severity="warn", headline="Some items were penalized.",
                     detail=f"{timeouts} timeout(s), {mismatches} mismatch(es).",
                     action="See the breakdown below.")


@router.get("/miner-dispatch")
async def dashboard_miner_dispatch(hotkey: Optional[str] = None):
    """Adaptive-dispatch status for the miner dashboard (RFC 2026-06-28).

    Display-only, consensus-decoupled. With ?hotkey=<ss58> returns that miner's
    status across every reporting validator (one row per validator). Without it,
    returns every validator's full latest snapshot. Empty until a validator running
    adaptive dispatch has flushed at least once.
    """
    import dispatch_status_store
    if hotkey:
        return {"hotkey": hotkey, "validators": dispatch_status_store.get_for_miner(hotkey)}
    return {"validators": dispatch_status_store.get_all()}


@router.get("/stats", response_model=DashboardStatsResponse)
async def dashboard_stats():
    """
    Public dashboard overview statistics.

    Returns total/analyzed/today counts for tweets, telegram messages, and
    articles, the latest analysis timestamp, and a sentiment distribution
    across all analysed content.
    """
    prisma = _get_prisma()
    try:
        # --- total counts ---
        tweets_total_rows = await prisma.query_raw("SELECT COUNT(*)::int AS cnt FROM tweets")
        telegram_total_rows = await prisma.query_raw("SELECT COUNT(*)::int AS cnt FROM telegram_messages")
        articles_total_rows = await prisma.query_raw("SELECT COUNT(*)::int AS cnt FROM news_articles")

        tweets_total = tweets_total_rows[0]["cnt"] if tweets_total_rows else 0
        telegram_total = telegram_total_rows[0]["cnt"] if telegram_total_rows else 0
        articles_total = articles_total_rows[0]["cnt"] if articles_total_rows else 0

        # --- analyzed counts ---
        tweets_analyzed_rows = await prisma.query_raw("SELECT COUNT(*)::int AS cnt FROM tweet_analysis")
        telegram_analyzed_rows = await prisma.query_raw("SELECT COUNT(*)::int AS cnt FROM telegram_message_analysis")
        articles_analyzed_rows = await prisma.query_raw("SELECT COUNT(*)::int AS cnt FROM news_article_analysis")

        tweets_analyzed = tweets_analyzed_rows[0]["cnt"] if tweets_analyzed_rows else 0
        telegram_analyzed = telegram_analyzed_rows[0]["cnt"] if telegram_analyzed_rows else 0
        articles_analyzed = articles_analyzed_rows[0]["cnt"] if articles_analyzed_rows else 0

        # --- analyzed today ---
        tweets_today_rows = await prisma.query_raw(
            "SELECT COUNT(*)::int AS cnt FROM tweet_analysis WHERE analyzed_at >= CURRENT_DATE"
        )
        telegram_today_rows = await prisma.query_raw(
            "SELECT COUNT(*)::int AS cnt FROM telegram_message_analysis WHERE analyzed_at >= CURRENT_DATE"
        )
        articles_today_rows = await prisma.query_raw(
            "SELECT COUNT(*)::int AS cnt FROM news_article_analysis WHERE analyzed_at >= CURRENT_DATE"
        )

        tweets_today = tweets_today_rows[0]["cnt"] if tweets_today_rows else 0
        telegram_today = telegram_today_rows[0]["cnt"] if telegram_today_rows else 0
        articles_today = articles_today_rows[0]["cnt"] if articles_today_rows else 0

        # --- latest analysis timestamp (max across all three tables) ---
        latest_rows = await prisma.query_raw("""
            SELECT MAX(latest) AS latest FROM (
                SELECT MAX(analyzed_at) AS latest FROM tweet_analysis
                UNION ALL
                SELECT MAX(analyzed_at) AS latest FROM telegram_message_analysis
                UNION ALL
                SELECT MAX(analyzed_at) AS latest FROM news_article_analysis
            ) sub
        """)
        latest_analysis_at = latest_rows[0]["latest"] if latest_rows and latest_rows[0]["latest"] else None

        # --- sentiment distribution across all sources ---
        sentiment_rows = await prisma.query_raw("""
            SELECT sentiment, SUM(cnt)::int AS total FROM (
                SELECT sentiment, COUNT(*) AS cnt FROM tweet_analysis WHERE sentiment IS NOT NULL GROUP BY sentiment
                UNION ALL
                SELECT sentiment, COUNT(*) AS cnt FROM telegram_message_analysis WHERE sentiment IS NOT NULL GROUP BY sentiment
                UNION ALL
                SELECT sentiment, COUNT(*) AS cnt FROM news_article_analysis WHERE sentiment IS NOT NULL GROUP BY sentiment
            ) sub
            GROUP BY sentiment
        """)
        sentiment_map: dict[str, int] = {}
        for row in (sentiment_rows or []):
            sentiment_map[row["sentiment"]] = row["total"]

        return DashboardStatsResponse(
            tweets=SourceCounts(total=tweets_total, analyzed=tweets_analyzed, analyzed_today=tweets_today),
            telegram=SourceCounts(total=telegram_total, analyzed=telegram_analyzed, analyzed_today=telegram_today),
            articles=SourceCounts(total=articles_total, analyzed=articles_analyzed, analyzed_today=articles_today),
            latest_analysis_at=latest_analysis_at,
            sentiment=SentimentDistribution(
                very_bullish=sentiment_map.get("very_bullish", 0),
                bullish=sentiment_map.get("bullish", 0),
                neutral=sentiment_map.get("neutral", 0),
                bearish=sentiment_map.get("bearish", 0),
                very_bearish=sentiment_map.get("very_bearish", 0),
            ),
        )
    except Exception as e:
        logger.error(f"Error in dashboard_stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# @router.get("/feed", response_model=FeedResponse)
async def dashboard_feed(
    page: int = 1,
    limit: int = 50,
    source_type: Optional[str] = None,
    sentiment: Optional[str] = None,
    asset: Optional[str] = None,
    impact: Optional[str] = None,
    q: Optional[str] = None,
    sort_by: str = "date",
    sort_order: str = "desc",
):
    """
    Unified paginated feed across tweets, telegram messages, and articles.

    Query params allow filtering by source_type, sentiment, asset symbol,
    impact potential, and full-text search on content.
    """
    prisma = _get_prisma()
    try:
        page = max(1, page)
        limit = max(1, min(limit, 200))
        offset = (page - 1) * limit

        # Validate sort
        sort_order_sql = "DESC" if sort_order.lower() == "desc" else "ASC"
        # For impact sorting we fall back to timestamp as secondary
        order_clause = "timestamp" if sort_by != "impact" else "impact_potential"

        # Parse source_type into a set for multi-value support
        source_types = {s.strip() for s in source_type.split(",") if s.strip()} if source_type else None

        # Build shared WHERE filters for each sub-query
        sentiment_list = [s.strip() for s in sentiment.split(",") if s.strip()] if sentiment else []
        asset_list = [a.strip() for a in asset.split(",") if a.strip()] if asset else []
        impact_list = [i.strip() for i in impact.split(",") if i.strip()] if impact else []
        search_term = f"%{q}%" if q else None

        parts: list[str] = []
        params: list = []
        param_idx = 1

        def _next_param(value):
            nonlocal param_idx
            params.append(value)
            idx = param_idx
            param_idx += 1
            return f"${idx}"

        # ---- Build filter clause fragments ----
        def _build_filters(sentiment_col: str, asset_col: str, impact_col: str, content_col: str):
            """Return a list of SQL conditions and update params/param_idx."""
            conditions = []
            if sentiment_list:
                placeholders = ", ".join(_next_param(s) for s in sentiment_list)
                conditions.append(f"{sentiment_col} IN ({placeholders})")
            if asset_list:
                placeholders = ", ".join(_next_param(a) for a in asset_list)
                conditions.append(f"{asset_col} IN ({placeholders})")
            if impact_list:
                placeholders = ", ".join(_next_param(i) for i in impact_list)
                conditions.append(f"{impact_col} IN ({placeholders})")
            if search_term:
                p = _next_param(search_term)
                conditions.append(f"{content_col} ILIKE {p}")
            return conditions

        # ---- Tweets sub-query ----
        if source_types is None or "tweet" in source_types:
            conds = _build_filters("ta.sentiment", "ta.asset_symbol", "ta.impact_potential", "t.text")
            where = " AND ".join(["ta.id IS NOT NULL"] + conds) if conds else "ta.id IS NOT NULL"
            parts.append(f"""
                SELECT
                    'tweet' AS source_type,
                    t.id::text AS item_id,
                    t.text AS content,
                    ta.sentiment,
                    ta.asset_symbol,
                    ta.content_type,
                    ta.impact_potential,
                    ta.technical_quality,
                    ta.market_analysis,
                    t.created_at AS timestamp,
                    a.screen_name AS author_screen_name,
                    a.profile_image_url AS author_profile_image_url,
                    NULL::text AS sender_username,
                    NULL::text AS sender_name,
                    NULL::text AS group_title,
                    NULL::text AS article_title,
                    NULL::text AS article_source,
                    NULL::text AS article_url,
                    NULL::text AS sector_symbol
                FROM tweets t
                JOIN tweet_analysis ta ON ta.tweet_id = t.id
                LEFT JOIN accounts a ON a.id = t.author_id
                WHERE {where}
            """)

        # ---- Telegram sub-query ----
        if source_types is None or "telegram" in source_types:
            conds = _build_filters("tma.sentiment", "tma.asset_symbol", "tma.impact_potential", "tm.content")
            where = " AND ".join(["tma.id IS NOT NULL"] + conds) if conds else "tma.id IS NOT NULL"
            parts.append(f"""
                SELECT
                    'telegram' AS source_type,
                    tm.id AS item_id,
                    tm.content,
                    tma.sentiment,
                    tma.asset_symbol,
                    tma.content_type,
                    tma.impact_potential,
                    tma.technical_quality,
                    tma.market_analysis,
                    tm.created_at AS timestamp,
                    NULL::text AS author_screen_name,
                    NULL::text AS author_profile_image_url,
                    tm.sender_username,
                    tm.sender_name,
                    tg.title AS group_title,
                    NULL::text AS article_title,
                    NULL::text AS article_source,
                    NULL::text AS article_url,
                    NULL::text AS sector_symbol
                FROM telegram_messages tm
                JOIN telegram_message_analysis tma ON tma.message_id = tm.id
                LEFT JOIN telegram_groups tg ON tg.id = tm.group_id
                WHERE {where}
            """)

        # ---- Articles sub-query ----
        if source_types is None or "article" in source_types:
            # For articles, asset_symbol filter maps to sector_symbol
            conds = _build_filters("naa.sentiment", "naa.sector_symbol", "naa.impact_potential", "na.title")
            where = " AND ".join(["naa.id IS NOT NULL"] + conds) if conds else "naa.id IS NOT NULL"
            parts.append(f"""
                SELECT
                    'article' AS source_type,
                    na.id::text AS item_id,
                    COALESCE(na.summary, na.title) AS content,
                    naa.sentiment,
                    naa.sector_symbol AS asset_symbol,
                    naa.content_type,
                    naa.impact_potential,
                    naa.technical_quality,
                    naa.market_analysis,
                    COALESCE(na.published, na.created_at) AS timestamp,
                    NULL::text AS author_screen_name,
                    NULL::text AS author_profile_image_url,
                    NULL::text AS sender_username,
                    NULL::text AS sender_name,
                    NULL::text AS group_title,
                    na.title AS article_title,
                    na.source AS article_source,
                    na.url AS article_url,
                    naa.sector_symbol
                FROM news_articles na
                JOIN news_article_analysis naa ON naa.article_id = na.id
                WHERE {where}
            """)

        if not parts:
            return FeedResponse(items=[], total=0, page=page, limit=limit, has_more=False)

        union_query = " UNION ALL ".join(parts)

        # Count total
        count_sql = f"SELECT COUNT(*)::int AS cnt FROM ({union_query}) AS feed"
        count_rows = await prisma.query_raw(count_sql, *params)
        total = count_rows[0]["cnt"] if count_rows else 0

        # Fetch page
        p_limit = _next_param(limit)
        p_offset = _next_param(offset)
        data_sql = f"""
            SELECT * FROM ({union_query}) AS feed
            ORDER BY {order_clause} {sort_order_sql} NULLS LAST
            LIMIT {p_limit} OFFSET {p_offset}
        """
        rows = await prisma.query_raw(data_sql, *params)

        items: list[FeedItem] = []
        for row in (rows or []):
            item = FeedItem(
                source_type=row["source_type"],
                id=row.get("item_id"),
                content=row.get("content"),
                sentiment=row.get("sentiment"),
                asset_symbol=row.get("asset_symbol"),
                content_type=row.get("content_type"),
                impact_potential=row.get("impact_potential"),
                technical_quality=row.get("technical_quality"),
                market_analysis=row.get("market_analysis"),
                timestamp=row.get("timestamp"),
            )
            if row["source_type"] == "tweet":
                item.author = FeedItemAuthor(
                    screen_name=row.get("author_screen_name"),
                    profile_image_url=row.get("author_profile_image_url"),
                )
            elif row["source_type"] == "telegram":
                item.telegram = FeedItemTelegramMeta(
                    sender_username=row.get("sender_username"),
                    sender_name=row.get("sender_name"),
                    group_title=row.get("group_title"),
                )
            elif row["source_type"] == "article":
                item.article = FeedItemArticleMeta(
                    title=row.get("article_title"),
                    source=row.get("article_source"),
                    url=row.get("article_url"),
                    sector_symbol=row.get("sector_symbol"),
                )
            items.append(item)

        return FeedResponse(
            items=items,
            total=total,
            page=page,
            limit=limit,
            has_more=(offset + limit) < total,
        )
    except Exception as e:
        logger.error(f"Error in dashboard_feed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


PREVIEW_LIMIT = 5
PREVIEW_DELAY = "3 hours"  # constant, not user input
RECENT_ITEMS_LIMIT = 10  # miner-profile recent items (same delay as PREVIEW_DELAY)


@router.get("/preview", response_model=FeedResponse)
async def dashboard_preview(
    source_type: Optional[str] = None,
    asset: Optional[str] = None,
):
    """Recent items preview."""
    prisma = _get_prisma()
    try:
        source_types = {s.strip() for s in source_type.split(",") if s.strip()} if source_type else None
        asset_list = [a.strip() for a in asset.split(",") if a.strip()] if asset else []

        params: list = []
        param_idx = 1

        def _next_param(value):
            nonlocal param_idx
            params.append(value)
            idx = param_idx
            param_idx += 1
            return f"${idx}"

        def _asset_filter(asset_col: str) -> str:
            if not asset_list:
                return ""
            placeholders = ", ".join(_next_param(a) for a in asset_list)
            return f" AND {asset_col} IN ({placeholders})"

        parts: list[str] = []
        if source_types is None or "tweet" in source_types:
            parts.append(f"""
                SELECT 'tweet' AS source_type, t.id::text AS item_id, t.text AS content,
                    ta.sentiment, ta.asset_symbol, ta.content_type, ta.impact_potential,
                    ta.technical_quality, ta.market_analysis, t.created_at AS timestamp,
                    a.screen_name AS author_screen_name, a.profile_image_url AS author_profile_image_url,
                    NULL::text AS sender_username, NULL::text AS sender_name, NULL::text AS group_title,
                    NULL::text AS article_title, NULL::text AS article_source, NULL::text AS article_url, NULL::text AS sector_symbol
                FROM tweets t JOIN tweet_analysis ta ON ta.tweet_id = t.id
                LEFT JOIN accounts a ON a.id = t.author_id
                WHERE ta.id IS NOT NULL{_asset_filter("ta.asset_symbol")}
            """)
        if source_types is None or "telegram" in source_types:
            parts.append(f"""
                SELECT 'telegram' AS source_type, tm.id AS item_id, tm.content,
                    tma.sentiment, tma.asset_symbol, tma.content_type, tma.impact_potential,
                    tma.technical_quality, tma.market_analysis, tm.created_at AS timestamp,
                    NULL::text AS author_screen_name, NULL::text AS author_profile_image_url,
                    tm.sender_username, tm.sender_name, tg.title AS group_title,
                    NULL::text AS article_title, NULL::text AS article_source, NULL::text AS article_url, NULL::text AS sector_symbol
                FROM telegram_messages tm JOIN telegram_message_analysis tma ON tma.message_id = tm.id
                LEFT JOIN telegram_groups tg ON tg.id = tm.group_id
                WHERE tma.id IS NOT NULL{_asset_filter("tma.asset_symbol")}
            """)
        if source_types is None or "article" in source_types:
            parts.append(f"""
                SELECT 'article' AS source_type, na.id::text AS item_id, COALESCE(na.summary, na.title) AS content,
                    naa.sentiment, naa.sector_symbol AS asset_symbol, naa.content_type, naa.impact_potential,
                    naa.technical_quality, naa.market_analysis, COALESCE(na.published, na.created_at) AS timestamp,
                    NULL::text AS author_screen_name, NULL::text AS author_profile_image_url,
                    NULL::text AS sender_username, NULL::text AS sender_name, NULL::text AS group_title,
                    na.title AS article_title, na.source AS article_source, na.url AS article_url, naa.sector_symbol
                FROM news_articles na JOIN news_article_analysis naa ON naa.article_id = na.id
                WHERE naa.id IS NOT NULL{_asset_filter("naa.sector_symbol")}
            """)

        if not parts:
            return FeedResponse(items=[], total=0, page=1, limit=PREVIEW_LIMIT, has_more=False)

        union_query = " UNION ALL ".join(parts)
        data_sql = f"""
            SELECT * FROM ({union_query}) AS feed
            WHERE timestamp <= NOW() - INTERVAL '{PREVIEW_DELAY}'
            ORDER BY timestamp DESC NULLS LAST
            LIMIT {PREVIEW_LIMIT}
        """
        rows = await prisma.query_raw(data_sql, *params)

        items: list[FeedItem] = []
        for row in (rows or []):
            item = FeedItem(
                source_type=row["source_type"],
                id=row.get("item_id"),
                content=row.get("content"),
                sentiment=row.get("sentiment"),
                asset_symbol=row.get("asset_symbol"),
                content_type=row.get("content_type"),
                impact_potential=row.get("impact_potential"),
                technical_quality=row.get("technical_quality"),
                market_analysis=row.get("market_analysis"),
                timestamp=row.get("timestamp"),
            )
            if row["source_type"] == "tweet":
                item.author = FeedItemAuthor(
                    screen_name=row.get("author_screen_name"),
                    profile_image_url=row.get("author_profile_image_url"),
                )
            elif row["source_type"] == "telegram":
                item.telegram = FeedItemTelegramMeta(
                    sender_username=row.get("sender_username"),
                    sender_name=row.get("sender_name"),
                    group_title=row.get("group_title"),
                )
            elif row["source_type"] == "article":
                item.article = FeedItemArticleMeta(
                    title=row.get("article_title"),
                    source=row.get("article_source"),
                    url=row.get("article_url"),
                    sector_symbol=row.get("sector_symbol"),
                )
            items.append(item)

        return FeedResponse(items=items, total=len(items), page=1, limit=PREVIEW_LIMIT, has_more=False)
    except Exception as e:
        logger.error(f"Error in dashboard_preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/articles/sources", response_model=ArticleSourcesResponse)
async def dashboard_article_sources():
    """
    Aggregated stats per news source: total articles, sentiment breakdown.

    NOTE: This route is registered BEFORE /dashboard/articles/{article_id}
    so that FastAPI matches the literal path first.
    """
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw("""
            SELECT
                na.source,
                COUNT(*)::int AS total_articles,
                COUNT(naa.id)::int AS analyzed_articles,
                COUNT(*) FILTER (WHERE naa.sentiment = 'very_bullish')::int AS very_bullish,
                COUNT(*) FILTER (WHERE naa.sentiment = 'bullish')::int AS bullish,
                COUNT(*) FILTER (WHERE naa.sentiment = 'neutral')::int AS neutral,
                COUNT(*) FILTER (WHERE naa.sentiment = 'bearish')::int AS bearish,
                COUNT(*) FILTER (WHERE naa.sentiment = 'very_bearish')::int AS very_bearish
            FROM news_articles na
            LEFT JOIN news_article_analysis naa ON naa.article_id = na.id
            GROUP BY na.source
            ORDER BY total_articles DESC
        """)

        # Twitter + Telegram as aggregate "sources" (sentiment over analyzed items).
        channel_rows = await prisma.query_raw("""
            SELECT 'Twitter/X' AS source, COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE sentiment = 'very_bullish')::int AS very_bullish,
                COUNT(*) FILTER (WHERE sentiment = 'bullish')::int AS bullish,
                COUNT(*) FILTER (WHERE sentiment = 'neutral')::int AS neutral,
                COUNT(*) FILTER (WHERE sentiment = 'bearish')::int AS bearish,
                COUNT(*) FILTER (WHERE sentiment = 'very_bearish')::int AS very_bearish
            FROM tweet_analysis
            UNION ALL
            SELECT 'Telegram', COUNT(*)::int,
                COUNT(*) FILTER (WHERE sentiment = 'very_bullish')::int,
                COUNT(*) FILTER (WHERE sentiment = 'bullish')::int,
                COUNT(*) FILTER (WHERE sentiment = 'neutral')::int,
                COUNT(*) FILTER (WHERE sentiment = 'bearish')::int,
                COUNT(*) FILTER (WHERE sentiment = 'very_bearish')::int
            FROM telegram_message_analysis
        """)

        sources = [
            SourceStats(
                source=row["source"],
                total_articles=row["total"],
                analyzed_articles=row["total"],
                very_bullish=row["very_bullish"],
                bullish=row["bullish"],
                neutral=row["neutral"],
                bearish=row["bearish"],
                very_bearish=row["very_bearish"],
            )
            for row in (channel_rows or [])
        ] + [
            SourceStats(
                source=row["source"],
                total_articles=row["total_articles"],
                analyzed_articles=row["analyzed_articles"],
                very_bullish=row["very_bullish"],
                bullish=row["bullish"],
                neutral=row["neutral"],
                bearish=row["bearish"],
                very_bearish=row["very_bearish"],
            )
            for row in (rows or [])
        ]
        return ArticleSourcesResponse(sources=sources)
    except Exception as e:
        logger.error(f"Error in dashboard_article_sources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# @router.get("/articles", response_model=ArticlesResponse)
async def dashboard_articles(
    page: int = 1,
    limit: int = 50,
    sentiment: Optional[str] = None,
    source: Optional[str] = None,
    sector: Optional[str] = None,
    impact: Optional[str] = None,
    q: Optional[str] = None,
    sort_by: str = "date",
    sort_order: str = "desc",
):
    """
    Paginated articles with analysis data joined.
    """
    prisma = _get_prisma()
    try:
        page = max(1, page)
        limit = max(1, min(limit, 200))
        offset = (page - 1) * limit
        sort_order_sql = "DESC" if sort_order.lower() == "desc" else "ASC"

        # Map sort_by to column
        sort_col_map = {
            "date": "COALESCE(na.published, na.created_at)",
            "sentiment": "naa.sentiment",
            "impact": "naa.impact_potential",
            "source": "na.source",
        }
        order_col = sort_col_map.get(sort_by, sort_col_map["date"])

        conditions: list[str] = []
        params: list = []
        param_idx = 1

        def _next_param(value):
            nonlocal param_idx
            params.append(value)
            idx = param_idx
            param_idx += 1
            return f"${idx}"

        if sentiment:
            vals = [s.strip() for s in sentiment.split(",") if s.strip()]
            if vals:
                placeholders = ", ".join(_next_param(v) for v in vals)
                conditions.append(f"naa.sentiment IN ({placeholders})")

        if source:
            vals = [s.strip() for s in source.split(",") if s.strip()]
            if vals:
                placeholders = ", ".join(_next_param(v) for v in vals)
                conditions.append(f"na.source IN ({placeholders})")

        if sector:
            vals = [s.strip() for s in sector.split(",") if s.strip()]
            if vals:
                placeholders = ", ".join(_next_param(v) for v in vals)
                conditions.append(f"naa.sector_symbol IN ({placeholders})")

        if impact:
            vals = [i.strip() for i in impact.split(",") if i.strip()]
            if vals:
                placeholders = ", ".join(_next_param(v) for v in vals)
                conditions.append(f"naa.impact_potential IN ({placeholders})")

        if q:
            p = _next_param(f"%{q}%")
            conditions.append(f"(na.title ILIKE {p} OR na.summary ILIKE {p})")

        where_clause = (" AND " + " AND ".join(conditions)) if conditions else ""

        # INNER JOIN: only surface analyzed articles (matches the /feed behavior).
        # Unanalyzed rows are the newest (analysis lags scraping) and would otherwise
        # top the date-sorted list with empty sentiment/impact/type.
        base_query = f"""
            FROM news_articles na
            JOIN news_article_analysis naa ON naa.article_id = na.id
            WHERE 1=1 {where_clause}
        """

        count_sql = f"SELECT COUNT(*)::int AS cnt {base_query}"
        count_rows = await prisma.query_raw(count_sql, *params)
        total = count_rows[0]["cnt"] if count_rows else 0

        p_limit = _next_param(limit)
        p_offset = _next_param(offset)

        data_sql = f"""
            SELECT
                na.id,
                na.url,
                na.title,
                na.summary,
                na.content,
                na.published,
                na.source,
                na.topic,
                na.created_at,
                naa.sentiment,
                naa.sector_symbol,
                naa.content_type,
                naa.technical_quality,
                naa.market_analysis,
                naa.impact_potential,
                naa.mentioned_assets,
                naa.analyzed_at
            {base_query}
            ORDER BY {order_col} {sort_order_sql} NULLS LAST
            LIMIT {p_limit} OFFSET {p_offset}
        """
        rows = await prisma.query_raw(data_sql, *params)

        articles = [
            ArticleWithAnalysis(
                id=row["id"],
                url=row["url"],
                title=row["title"],
                summary=row.get("summary"),
                content=row.get("content"),
                published=row.get("published"),
                source=row["source"],
                topic=row.get("topic"),
                created_at=row.get("created_at"),
                sentiment=row.get("sentiment"),
                sector_symbol=row.get("sector_symbol"),
                content_type=row.get("content_type"),
                technical_quality=row.get("technical_quality"),
                market_analysis=row.get("market_analysis"),
                impact_potential=row.get("impact_potential"),
                mentioned_assets=row.get("mentioned_assets"),
                analyzed_at=row.get("analyzed_at"),
            )
            for row in (rows or [])
        ]

        return ArticlesResponse(
            articles=articles,
            total=total,
            page=page,
            limit=limit,
            has_more=(offset + limit) < total,
        )
    except Exception as e:
        logger.error(f"Error in dashboard_articles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# @router.get("/articles/{article_id}", response_model=ArticleDetailResponse)
async def dashboard_article_detail(article_id: int):
    """
    Single article detail with full analysis.
    """
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
            SELECT
                na.id,
                na.url,
                na.title,
                na.summary,
                na.content,
                na.published,
                na.source,
                na.topic,
                na.created_at,
                naa.sentiment,
                naa.sector_symbol,
                naa.content_type,
                naa.technical_quality,
                naa.market_analysis,
                naa.impact_potential,
                naa.relevance_confidence,
                naa.mentioned_assets,
                naa.analyzed_at,
                naa.miner_hotkey
            FROM news_articles na
            LEFT JOIN news_article_analysis naa ON naa.article_id = na.id
            WHERE na.id = $1
            """,
            article_id,
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Article not found")

        row = rows[0]
        return ArticleDetailResponse(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            summary=row.get("summary"),
            content=row.get("content"),
            published=row.get("published"),
            source=row["source"],
            topic=row.get("topic"),
            created_at=row.get("created_at"),
            sentiment=row.get("sentiment"),
            sector_symbol=row.get("sector_symbol"),
            content_type=row.get("content_type"),
            technical_quality=row.get("technical_quality"),
            market_analysis=row.get("market_analysis"),
            impact_potential=row.get("impact_potential"),
            relevance_confidence=row.get("relevance_confidence"),
            mentioned_assets=row.get("mentioned_assets"),
            analyzed_at=row.get("analyzed_at"),
            miner_hotkey=row.get("miner_hotkey"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in dashboard_article_detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# @router.get("/tweets/{tweet_id}", response_model=TweetDetailResponse)
async def dashboard_tweet_detail(tweet_id: int):
    """Single tweet detail with author and analysis."""
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
            SELECT
                t.id, t.text, t.url, t.lang,
                t.retweet_count, t.reply_count, t.like_count, t.quote_count,
                t.view_count, t.bookmark_count, t.is_reply, t.created_at,
                a.screen_name AS author_screen_name,
                a.name AS author_name,
                a.profile_image_url AS author_profile_image_url,
                a.followers_count AS author_followers_count,
                a.verified AS author_verified,
                ta.sentiment, ta.asset_id, ta.asset_symbol,
                ta.content_type, ta.technical_quality, ta.market_analysis,
                ta.impact_potential, ta.relevance_confidence, ta.analyzed_at,
                ta.miner_hotkey
            FROM tweets t
            LEFT JOIN accounts a ON a.id = t.author_id
            LEFT JOIN tweet_analysis ta ON ta.tweet_id = t.id
            WHERE t.id = $1
            """,
            tweet_id,
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Tweet not found")
        row = rows[0]
        return TweetDetailResponse(
            id=str(row["id"]),
            text=row.get("text"),
            url=row.get("url"),
            lang=row.get("lang"),
            retweet_count=row.get("retweet_count", 0) or 0,
            reply_count=row.get("reply_count", 0) or 0,
            like_count=row.get("like_count", 0) or 0,
            quote_count=row.get("quote_count", 0) or 0,
            view_count=row.get("view_count", 0) or 0,
            bookmark_count=row.get("bookmark_count", 0) or 0,
            is_reply=row.get("is_reply", False) or False,
            created_at=row.get("created_at"),
            author_screen_name=row.get("author_screen_name"),
            author_name=row.get("author_name"),
            author_profile_image_url=row.get("author_profile_image_url"),
            author_followers_count=row.get("author_followers_count", 0) or 0,
            author_verified=row.get("author_verified", False) or False,
            sentiment=row.get("sentiment"),
            asset_id=row.get("asset_id"),
            asset_symbol=row.get("asset_symbol"),
            content_type=row.get("content_type"),
            technical_quality=row.get("technical_quality"),
            market_analysis=row.get("market_analysis"),
            impact_potential=row.get("impact_potential"),
            relevance_confidence=row.get("relevance_confidence"),
            analyzed_at=row.get("analyzed_at"),
            miner_hotkey=row.get("miner_hotkey"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in dashboard_tweet_detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# @router.get("/telegram/{message_id}", response_model=TelegramDetailResponse)
async def dashboard_telegram_detail(message_id: str):
    """Single telegram message detail with group and analysis."""
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
            SELECT
                tm.id, tm.telegram_id, tm.content,
                tm.sender_username, tm.sender_name,
                tm.reply_to_id, tm.created_at,
                tg.title AS group_title, tg.telegram_id AS group_telegram_id,
                tma.sentiment, tma.asset_id, tma.asset_symbol,
                tma.content_type, tma.technical_quality, tma.market_analysis,
                tma.impact_potential, tma.relevance_confidence, tma.analyzed_at,
                tma.miner_hotkey
            FROM telegram_messages tm
            LEFT JOIN telegram_groups tg ON tg.id = tm.group_id
            LEFT JOIN telegram_message_analysis tma ON tma.message_id = tm.id
            WHERE tm.id = $1
            """,
            message_id,
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Message not found")
        row = rows[0]
        return TelegramDetailResponse(
            id=row["id"],
            telegram_id=row.get("telegram_id"),
            content=row.get("content"),
            sender_username=row.get("sender_username"),
            sender_name=row.get("sender_name"),
            group_title=row.get("group_title"),
            group_telegram_id=str(row["group_telegram_id"]) if row.get("group_telegram_id") else None,
            reply_to_id=row.get("reply_to_id"),
            created_at=row.get("created_at"),
            sentiment=row.get("sentiment"),
            asset_id=row.get("asset_id"),
            asset_symbol=row.get("asset_symbol"),
            content_type=row.get("content_type"),
            technical_quality=row.get("technical_quality"),
            market_analysis=row.get("market_analysis"),
            impact_potential=row.get("impact_potential"),
            relevance_confidence=row.get("relevance_confidence"),
            analyzed_at=row.get("analyzed_at"),
            miner_hotkey=row.get("miner_hotkey"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in dashboard_telegram_detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sentiment", response_model=SentimentResponse)
async def dashboard_sentiment(
    asset: Optional[str] = None,
    source_type: Optional[str] = None,
    days: int = 7,
):
    """
    Sentiment distribution grouped by day and overall.

    Optionally filter by asset symbol and/or source type.
    """
    prisma = _get_prisma()
    try:
        days = max(1, min(days, 365))
        # Parse source_type into a set for multi-value support (comma-separated)
        source_types = {s.strip() for s in source_type.split(",") if s.strip()} if source_type else None
        parts: list[str] = []
        params: list = []
        param_idx = 1

        def _next_param(value):
            nonlocal param_idx
            params.append(value)
            idx = param_idx
            param_idx += 1
            return f"${idx}"

        p_days = _next_param(days)

        # Build sub-queries per source type
        if source_types is None or "tweet" in source_types:
            cond = f"ta.analyzed_at >= CURRENT_DATE - ({p_days} || ' days')::interval AND ta.sentiment IS NOT NULL"
            if asset:
                p = _next_param(asset)
                cond += f" AND ta.asset_symbol = {p}"
            parts.append(f"""
                SELECT ta.analyzed_at::date AS day, ta.sentiment
                FROM tweet_analysis ta
                WHERE {cond}
            """)

        if source_types is None or "telegram" in source_types:
            cond = f"tma.analyzed_at >= CURRENT_DATE - ({p_days} || ' days')::interval AND tma.sentiment IS NOT NULL"
            if asset:
                p = _next_param(asset)
                cond += f" AND tma.asset_symbol = {p}"
            parts.append(f"""
                SELECT tma.analyzed_at::date AS day, tma.sentiment
                FROM telegram_message_analysis tma
                WHERE {cond}
            """)

        if source_types is None or "article" in source_types:
            cond = f"naa.analyzed_at >= CURRENT_DATE - ({p_days} || ' days')::interval AND naa.sentiment IS NOT NULL"
            if asset:
                p = _next_param(asset)
                cond += f" AND naa.sector_symbol = {p}"
            parts.append(f"""
                SELECT naa.analyzed_at::date AS day, naa.sentiment
                FROM news_article_analysis naa
                WHERE {cond}
            """)

        if not parts:
            return SentimentResponse(
                overall=SentimentDistribution(),
                daily=[],
                days=days,
                source_type=source_type,
                asset=asset,
            )

        union_query = " UNION ALL ".join(parts)

        # Overall sentiment
        overall_sql = f"""
            SELECT sentiment, COUNT(*)::int AS cnt
            FROM ({union_query}) AS combined
            GROUP BY sentiment
        """
        overall_rows = await prisma.query_raw(overall_sql, *params)
        overall_map: dict[str, int] = {}
        for row in (overall_rows or []):
            overall_map[row["sentiment"]] = row["cnt"]

        # Daily sentiment
        daily_sql = f"""
            SELECT
                day,
                COUNT(*) FILTER (WHERE sentiment = 'very_bullish')::int AS very_bullish,
                COUNT(*) FILTER (WHERE sentiment = 'bullish')::int AS bullish,
                COUNT(*) FILTER (WHERE sentiment = 'neutral')::int AS neutral,
                COUNT(*) FILTER (WHERE sentiment = 'bearish')::int AS bearish,
                COUNT(*) FILTER (WHERE sentiment = 'very_bearish')::int AS very_bearish
            FROM ({union_query}) AS combined
            GROUP BY day
            ORDER BY day ASC
        """
        # We need to pass params twice since union_query appears twice
        daily_rows = await prisma.query_raw(daily_sql, *params)

        daily = [
            DailySentiment(
                date=str(row["day"]),
                very_bullish=row["very_bullish"],
                bullish=row["bullish"],
                neutral=row["neutral"],
                bearish=row["bearish"],
                very_bearish=row["very_bearish"],
            )
            for row in (daily_rows or [])
        ]

        return SentimentResponse(
            overall=SentimentDistribution(
                very_bullish=overall_map.get("very_bullish", 0),
                bullish=overall_map.get("bullish", 0),
                neutral=overall_map.get("neutral", 0),
                bearish=overall_map.get("bearish", 0),
                very_bearish=overall_map.get("very_bearish", 0),
            ),
            daily=daily,
            days=days,
            source_type=source_type,
            asset=asset,
        )
    except Exception as e:
        logger.error(f"Error in dashboard_sentiment: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Miner Leaderboard & Profile Endpoints
# ============================================================================

# The leaderboard aggregation scans every analysis row and de-TOASTs article
# content to score articles by length (~12s over ~1M items). Running that on
# each request made the page intermittently time out. We instead compute the
# full set at most once per TTL and serve every sort/limit variant from that
# snapshot (sorting/slicing in Python). Stale-while-revalidate: a stale hit is
# returned immediately while a single background task refreshes.
# Freshness normally comes from the periodic warm in jobs/background_tasks
# (every 15 min); this TTL is only the request-path fallback if that loop
# dies. The aggregation costs ~45s of DB time, so keep the cadence low.
_MINERS_CACHE_TTL = 1200.0
_miners_cache = {"rows": None, "ts": 0.0}
_miners_refresh_lock = asyncio.Lock()

_MINERS_AGG_SQL = """
    WITH miner_stats AS (
        SELECT
            miner_hotkey,
            COUNT(*) AS total_items,
            SUM(CASE WHEN source = 'tweet' THEN 1 ELSE 0 END) AS tweet_count,
            SUM(CASE WHEN source = 'telegram' THEN 1 ELSE 0 END) AS telegram_count,
            SUM(CASE WHEN source = 'article' THEN 1 ELSE 0 END) AS article_count,
            SUM(CASE WHEN sentiment = 'very_bullish' THEN 1 ELSE 0 END) AS very_bullish,
            SUM(CASE WHEN sentiment = 'bullish' THEN 1 ELSE 0 END) AS bullish,
            SUM(CASE WHEN sentiment = 'neutral' THEN 1 ELSE 0 END) AS neutral,
            SUM(CASE WHEN sentiment = 'bearish' THEN 1 ELSE 0 END) AS bearish,
            SUM(CASE WHEN sentiment = 'very_bearish' THEN 1 ELSE 0 END) AS very_bearish,
            -- True per-item points from the scoring rule (tweet/telegram = 1;
            -- article = 1/2/3 by content length, per neurons/validator.py).
            SUM(points) AS total_points,
            COALESCE(SUM(points) FILTER (WHERE analyzed_at >= now() - interval '24 hours'), 0) AS points_24h,
            COALESCE(SUM(points) FILTER (WHERE analyzed_at >= now() - interval '7 days') / 7.0, 0) AS avg_points_day,
            MIN(analyzed_at) AS first_seen,
            MAX(analyzed_at) AS last_seen
        FROM (
            SELECT miner_hotkey, sentiment, analyzed_at, 'tweet' AS source, 1 AS points
            FROM tweet_analysis WHERE miner_hotkey IS NOT NULL
            UNION ALL
            SELECT miner_hotkey, sentiment, analyzed_at, 'telegram' AS source, 1 AS points
            FROM telegram_message_analysis WHERE miner_hotkey IS NOT NULL
            UNION ALL
            SELECT naa.miner_hotkey, naa.sentiment, naa.analyzed_at, 'article' AS source,
                   CASE WHEN length(na.content) >= 2000 THEN 3
                        WHEN length(na.content) >= 500  THEN 2
                        ELSE 1 END AS points
            FROM news_article_analysis naa
            JOIN news_articles na ON na.id = naa.article_id
            WHERE naa.miner_hotkey IS NOT NULL
        ) AS all_analyses
        GROUP BY miner_hotkey
    )
    SELECT ms.*, ms.total_points AS total_rewards
    FROM miner_stats ms
"""


async def _refresh_miners_cache(prisma) -> None:
    """Recompute the leaderboard snapshot. At most one runs at a time; while it
    is in flight the previous (stale) snapshot keeps serving."""
    if _miners_refresh_lock.locked():
        return
    async with _miners_refresh_lock:
        try:
            rows = await prisma.query_raw(_MINERS_AGG_SQL)
            _miners_cache["rows"] = rows
            _miners_cache["ts"] = time.monotonic()
        except Exception as e:
            # repr, not str: the common failure is an httpx timeout whose
            # str() is empty, which once hid the root cause of a blank page.
            logger.error(f"miners cache refresh failed: {e!r}")


async def refresh_miners_snapshot(prisma) -> None:
    """Public entrypoint for the periodic warm in jobs/background_tasks.
    Keeps the leaderboard snapshot fresh so no user request ever waits on
    the ~45s aggregation."""
    await _refresh_miners_cache(prisma)


async def _get_miner_rows(prisma):
    """Return the cached leaderboard rows without ever blocking on the
    aggregation: a missing or stale snapshot kicks a background refresh and
    serves what we have (empty only in the first minute after a restart,
    until the startup warm lands)."""
    age = time.monotonic() - _miners_cache["ts"]
    if _miners_cache["rows"] is None or age > _MINERS_CACHE_TTL:
        asyncio.create_task(_refresh_miners_cache(prisma))
    return _miners_cache["rows"] or []


def _recent_sort_key(last_seen) -> float:
    """Epoch seconds for a last_seen value that query_raw may return as a
    datetime or an ISO string."""
    if last_seen is None:
        return 0.0
    if hasattr(last_seen, "timestamp"):
        return last_seen.timestamp()
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(last_seen).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


@router.get("/miners", response_model=MinerLeaderboardResponse)
async def dashboard_miners(
    sort_by: Optional[str] = "total_items",
    limit: int = 50,
):
    """Miner leaderboard with aggregated stats across all source types.

    Served from a short-lived in-process snapshot (see _get_miner_rows): the
    aggregation is expensive, so it runs at most once per TTL and every
    sort/limit variant is derived from the same snapshot here in Python.
    """
    try:
        rows = await _get_miner_rows(_get_prisma())

        if sort_by == "total_rewards":
            key = lambda r: float(r.get("total_points") or 0)
        elif sort_by == "recent":
            key = lambda r: _recent_sort_key(r.get("last_seen"))
        else:
            key = lambda r: float(r.get("total_items") or 0)
        ordered = sorted(rows, key=key, reverse=True)[: max(0, limit)]

        miners = []
        for row in ordered:
            miners.append(MinerLeaderboardEntry(
                hotkey=row["miner_hotkey"],
                total_items=row["total_items"],
                tweet_count=row["tweet_count"],
                telegram_count=row["telegram_count"],
                article_count=row["article_count"],
                sentiment=SentimentDistribution(
                    very_bullish=row["very_bullish"],
                    bullish=row["bullish"],
                    neutral=row["neutral"],
                    bearish=row["bearish"],
                    very_bearish=row["very_bearish"],
                ),
                first_seen=row.get("first_seen"),
                last_seen=row.get("last_seen"),
                total_rewards=row.get("total_rewards", 0) or 0,
                points_24h=row.get("points_24h", 0) or 0,
                avg_points_day=row.get("avg_points_day", 0) or 0,
            ))

        return MinerLeaderboardResponse(miners=miners, total=len(miners))
    except Exception as e:
        logger.error(f"Error in dashboard_miners: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/miners/{hotkey}", response_model=MinerProfileResponse)
async def dashboard_miner_profile(hotkey: str):
    """Single miner profile with stats, recent items, and rewards."""
    prisma = _get_prisma()
    try:
        stats_rows = await prisma.query_raw(
            """
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE WHEN source = 'tweet' THEN 1 ELSE 0 END) AS tweet_count,
                SUM(CASE WHEN source = 'telegram' THEN 1 ELSE 0 END) AS telegram_count,
                SUM(CASE WHEN source = 'article' THEN 1 ELSE 0 END) AS article_count,
                SUM(CASE WHEN sentiment = 'very_bullish' THEN 1 ELSE 0 END) AS very_bullish,
                SUM(CASE WHEN sentiment = 'bullish' THEN 1 ELSE 0 END) AS bullish,
                SUM(CASE WHEN sentiment = 'neutral' THEN 1 ELSE 0 END) AS neutral,
                SUM(CASE WHEN sentiment = 'bearish' THEN 1 ELSE 0 END) AS bearish,
                SUM(CASE WHEN sentiment = 'very_bearish' THEN 1 ELSE 0 END) AS very_bearish,
                MIN(analyzed_at) AS first_seen,
                MAX(analyzed_at) AS last_seen
            FROM (
                SELECT sentiment, analyzed_at, 'tweet' AS source
                FROM tweet_analysis WHERE miner_hotkey = $1
                UNION ALL
                SELECT sentiment, analyzed_at, 'telegram' AS source
                FROM telegram_message_analysis WHERE miner_hotkey = $1
                UNION ALL
                SELECT sentiment, analyzed_at, 'article' AS source
                FROM news_article_analysis WHERE miner_hotkey = $1
            ) AS all_analyses
            """,
            hotkey,
        )

        if not stats_rows or stats_rows[0]["total_items"] == 0:
            raise HTTPException(status_code=404, detail="Miner not found")

        s = stats_rows[0]

        reward_rows = await prisma.query_raw(
            """
            SELECT start_block, stop_block, points, created_at
            FROM rewards
            WHERE hotkey = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            hotkey,
        )
        total_rewards = sum(r["points"] for r in reward_rows)

        penalty_rows = await prisma.query_raw(
            """
            SELECT reason, timestamp
            FROM penalties
            WHERE hotkey = $1
            ORDER BY timestamp DESC
            LIMIT 50
            """,
            hotkey,
        )

        recent_rows = await prisma.query_raw(
            f"""
            (
                SELECT 'tweet' AS source_type, ta.tweet_id::text AS id,
                    t.text AS content, ta.sentiment, ta.asset_symbol,
                    ta.impact_potential, ta.technical_quality, ta.analyzed_at
                FROM tweet_analysis ta
                JOIN tweets t ON t.id = ta.tweet_id
                WHERE ta.miner_hotkey = $1 AND ta.analyzed_at <= NOW() - INTERVAL '{PREVIEW_DELAY}'
                ORDER BY ta.analyzed_at DESC LIMIT {RECENT_ITEMS_LIMIT}
            )
            UNION ALL
            (
                SELECT 'telegram' AS source_type, tma.message_id AS id,
                    tm.content, tma.sentiment, tma.asset_symbol,
                    tma.impact_potential, tma.technical_quality, tma.analyzed_at
                FROM telegram_message_analysis tma
                JOIN telegram_messages tm ON tm.id = tma.message_id
                WHERE tma.miner_hotkey = $1 AND tma.analyzed_at <= NOW() - INTERVAL '{PREVIEW_DELAY}'
                ORDER BY tma.analyzed_at DESC LIMIT {RECENT_ITEMS_LIMIT}
            )
            UNION ALL
            (
                SELECT 'article' AS source_type, naa.article_id::text AS id,
                    na.title AS content, naa.sentiment, naa.sector_symbol AS asset_symbol,
                    naa.impact_potential, naa.technical_quality, naa.analyzed_at
                FROM news_article_analysis naa
                JOIN news_articles na ON na.id = naa.article_id
                WHERE naa.miner_hotkey = $1 AND naa.analyzed_at <= NOW() - INTERVAL '{PREVIEW_DELAY}'
                ORDER BY naa.analyzed_at DESC LIMIT {RECENT_ITEMS_LIMIT}
            )
            ORDER BY analyzed_at DESC
            LIMIT {RECENT_ITEMS_LIMIT}
            """,
            hotkey,
        )

        return MinerProfileResponse(
            hotkey=hotkey,
            total_items=s["total_items"],
            tweet_count=s["tweet_count"],
            telegram_count=s["telegram_count"],
            article_count=s["article_count"],
            sentiment=SentimentDistribution(
                very_bullish=s["very_bullish"],
                bullish=s["bullish"],
                neutral=s["neutral"],
                bearish=s["bearish"],
                very_bearish=s["very_bearish"],
            ),
            first_seen=s.get("first_seen"),
            last_seen=s.get("last_seen"),
            total_rewards=total_rewards,
            total_penalties=len(penalty_rows),
            recent_items=[
                MinerRecentItem(
                    source_type=r["source_type"],
                    id=r["id"],
                    content=r.get("content"),
                    sentiment=r.get("sentiment"),
                    asset_symbol=r.get("asset_symbol"),
                    impact_potential=r.get("impact_potential"),
                    technical_quality=r.get("technical_quality"),
                    analyzed_at=r.get("analyzed_at"),
                )
                for r in recent_rows
            ],
            rewards=[
                MinerRewardEntry(
                    start_block=r["start_block"],
                    stop_block=r["stop_block"],
                    points=r["points"],
                    created_at=r.get("created_at"),
                )
                for r in reward_rows
            ],
            penalties=[
                MinerPenaltyEntry(
                    reason=r["reason"],
                    timestamp=r.get("timestamp"),
                )
                for r in penalty_rows
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in dashboard_miner_profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Miner Batches (scoring transparency) — read-only, decoupled from consensus
# ============================================================================

@router.get("/miners/{hotkey}/batches", response_model=MinerBatchesResponse)
async def dashboard_miner_batches(hotkey: str):
    """Per-epoch statement line items for a miner: raw earned (pre-zeroing), penalty
    breakdown by cause, and a server-side diagnosis. Sources: score_verdict valid rows
    + the standalone penalty_detail table. Never reads attestation/Merkle."""
    prisma = _get_prisma()
    try:
        # Distinct items earned (valid) per epoch. COUNT(DISTINCT (type,id)) dedups the
        # same item scored by multiple validators (see §13.2 reconciliation). Equals raw
        # points while verdicts are uniformly 1.0 (true today).
        earned_rows = await prisma.query_raw(
            """
            SELECT epoch, COUNT(DISTINCT (resource_type, resource_id)) AS earned_items
            FROM score_verdict
            WHERE miner_hotkey = $1 AND validator_verdict = 'valid'
            GROUP BY epoch
            """,
            hotkey,
        )
        # Penalty items + how many distinct validators flagged the miner, per epoch.
        pen_rows = await prisma.query_raw(
            """
            SELECT epoch,
                   COUNT(DISTINCT (resource_type, resource_id)) AS penalty_items,
                   COUNT(DISTINCT validator_hotkey) AS flagged_by
            FROM penalty_detail
            WHERE miner_hotkey = $1
            GROUP BY epoch
            """,
            hotkey,
        )
        # Cause breakdown (distinct items per cause), per epoch.
        cause_rows = await prisma.query_raw(
            """
            SELECT epoch, cause, COUNT(DISTINCT (resource_type, resource_id)) AS cnt
            FROM penalty_detail
            WHERE miner_hotkey = $1
            GROUP BY epoch, cause
            """,
            hotkey,
        )
        # Authoritative on-chain outcome: the post-zeroing rewards table. No validator
        # column, so multiple rows per (hotkey, block); MAX is the conservative "most
        # generous" view. Keyed by start_block, mapped to epoch = start_block / BLOCK_LENGTH.
        reward_rows = await prisma.query_raw(
            """
            SELECT start_block, COUNT(*) AS n, MAX(points) AS max_points
            FROM rewards
            WHERE hotkey = $1
            GROUP BY start_block
            """,
            hotkey,
        )
        # Last activity time per epoch (for the "2h ago" column).
        activity_rows = await prisma.query_raw(
            """
            SELECT epoch, MAX(created_at) AS last_at FROM (
                SELECT epoch, created_at FROM score_verdict WHERE miner_hotkey = $1
                UNION ALL
                SELECT epoch, created_at FROM penalty_detail WHERE miner_hotkey = $1
            ) u
            GROUP BY epoch
            """,
            hotkey,
        )

        earned_by_epoch = {r["epoch"]: int(r["earned_items"]) for r in earned_rows}
        pen_by_epoch = {r["epoch"]: r for r in pen_rows}
        breakdown_by_epoch: dict = {}
        for r in cause_rows:
            breakdown_by_epoch.setdefault(r["epoch"], {})[r["cause"]] = int(r["cnt"])
        # reward_by_epoch[epoch] = {"n": rows_written, "max": max_points}
        reward_by_epoch: dict = {}
        if BLOCK_LENGTH > 0:
            for r in reward_rows:
                ep_r = int(r["start_block"]) // BLOCK_LENGTH
                reward_by_epoch[ep_r] = {"n": int(r["n"]),
                                         "max": float(r["max_points"]) if r["max_points"] is not None else 0.0}
        activity_by_epoch = {r["epoch"]: r.get("last_at") for r in activity_rows}

        all_epochs = sorted(
            set(earned_by_epoch) | set(pen_by_epoch) | set(breakdown_by_epoch),
            reverse=True,
        )[:50]

        batches = []
        for ep in all_epochs:
            start, stop = _block_window(int(ep))
            earned = earned_by_epoch.get(ep, 0)
            pen = pen_by_epoch.get(ep)
            penalty_items = int(pen["penalty_items"]) if pen else 0
            flagged_by = int(pen["flagged_by"]) if pen else 0
            breakdown = breakdown_by_epoch.get(ep, {})
            detail_coverage = ep in pen_by_epoch

            # On-chain outcome (3-way gate, per section 16.1). reward_points_max is None
            # until a reward row lands (rewards lag ~2 epochs) -> "pending", never "zeroed".
            rwd = reward_by_epoch.get(ep)
            reward_points_max = rwd["max"] if rwd else None
            was_zeroed = bool(rwd and rwd["n"] > 0 and rwd["max"] <= 0.0 and earned > 0)

            batches.append(MinerBatch(
                epoch=int(ep),
                block_window_start=start,
                block_window_stop=stop,
                last_activity_at=activity_by_epoch.get(ep),
                earned_items=earned,
                penalty_items=penalty_items,
                penalty_breakdown=breakdown,
                reward_points_max=reward_points_max,
                was_zeroed=was_zeroed,
                flagged_by_validators=flagged_by,
                detail_coverage=detail_coverage,
                diagnosis=_diagnose_batch(earned, penalty_items, breakdown, was_zeroed),
            ))

        return MinerBatchesResponse(hotkey=hotkey, block_length=BLOCK_LENGTH, batches=batches)
    except Exception as e:
        logger.error(f"Error in dashboard_miner_batches: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/miners/{hotkey}/batches/{epoch}/items", response_model=MinerBatchItemsResponse)
async def dashboard_miner_batch_items(hotkey: str, epoch: int):
    """Per-item penalty detail for one batch — the miner-vs-validator diff, joined to
    the item's content preview. Read-only from penalty_detail (+ content tables)."""
    prisma = _get_prisma()
    try:
        # Dedup by (resource_type, resource_id): at fleet scale the same item is flagged by
        # many validators (and the append-only table can hold repeats from one validator
        # reprocessing an item). One card per item, keeping the most recent row, so the
        # drawer matches the deduped penalty_items count on /batches. flagged_by_validators
        # already conveys "N validators agreed".
        rows = await prisma.query_raw(
            """
            SELECT resource_type, resource_id, cause, failed_fields,
                   miner_values, validator_values, post_preview, score, validator_hotkey
            FROM (
                SELECT DISTINCT ON (resource_type, resource_id)
                       resource_type, resource_id, cause, failed_fields,
                       miner_values, validator_values, post_preview, score, validator_hotkey, created_at
                FROM penalty_detail
                WHERE miner_hotkey = $1 AND epoch = $2
                ORDER BY resource_type, resource_id, created_at DESC
            ) d
            ORDER BY created_at DESC
            LIMIT 20
            """,
            hotkey,
            epoch,
        )
        # Earned (valid) items for this batch (section 18). DISTINCT ON (type,id) dedups the
        # same item scored by multiple validators and keeps the highest-points row — matches
        # how earned_items is counted on /batches. Read-only from score_verdict.
        EARNED_LIMIT = 10
        earned_rows = await prisma.query_raw(
            f"""
            SELECT DISTINCT ON (resource_type, resource_id)
                   resource_type, resource_id, points_awarded, categorical_key, validator_hotkey
            FROM score_verdict
            WHERE miner_hotkey = $1 AND epoch = $2 AND validator_verdict = 'valid'
                  AND created_at <= NOW() - INTERVAL '{PREVIEW_DELAY}'
            ORDER BY resource_type, resource_id, points_awarded DESC
            LIMIT $3
            """,
            hotkey,
            epoch,
            EARNED_LIMIT,
        )
        # score_verdict uses resource_type "news" for articles; penalty_detail uses "article".
        # Normalize the earned rows to the penalty_detail/frontend vocabulary so the whole
        # response is self-consistent (tweet | telegram | article) and the preview resolver matches.
        _RTYPE_NORM = {"news": "article"}
        earned_norm = [{**r, "resource_type": _RTYPE_NORM.get(r["resource_type"], r["resource_type"])}
                       for r in earned_rows]

        # Resolve a content preview per item from the type-specific content table, for BOTH
        # penalized and earned items in one pass. Small N per batch, so a per-type fetch
        # keyed by id is cheap and clear.
        by_type: dict = {}
        for r in rows:
            by_type.setdefault(r["resource_type"], set()).add(str(r["resource_id"]))
        for r in earned_norm:
            by_type.setdefault(r["resource_type"], set()).add(str(r["resource_id"]))

        # Best-effort content previews. A failure here (e.g. driver array binding) must
        # not 500 the endpoint — items still return with their stored post_preview.
        previews: dict = {}  # (resource_type, resource_id) -> preview text
        try:
            if by_type.get("tweet"):
                ids = list(by_type["tweet"])
                trows = await prisma.query_raw(
                    "SELECT id::text AS id, text FROM tweets WHERE id::text = ANY($1)", ids,
                )
                for t in trows:
                    previews[("tweet", t["id"])] = t.get("text")
            if by_type.get("telegram"):
                ids = list(by_type["telegram"])
                mrows = await prisma.query_raw(
                    "SELECT id, content FROM telegram_messages WHERE id = ANY($1)", ids,
                )
                for m in mrows:
                    previews[("telegram", str(m["id"]))] = m.get("content")
            if by_type.get("article"):
                ids = list(by_type["article"])
                arows = await prisma.query_raw(
                    "SELECT id::text AS id, title FROM news_articles WHERE id::text = ANY($1)", ids,
                )
                for a in arows:
                    previews[("article", a["id"])] = a.get("title")
        except Exception as e:
            logger.warning(f"batch_items preview resolution failed (non-fatal): {e}")

        # v2 richer analysis (display-only context) for article items — the entities/event/geo/
        # impact-level/source extraction a miner's node produces beyond the six scored fields.
        # One row per article in news_article_analysis; best-effort and never 500s the endpoint.
        analyses: dict = {}  # article_id (str) -> ItemAnalysis
        try:
            if by_type.get("article"):
                ids = list(by_type["article"])
                narows = await prisma.query_raw(
                    """
                    SELECT article_id::text AS article_id, impact_level, event_type, event_date,
                           primary_geo, factual_confidence, overall_sentiment_score,
                           miner_hotkey, analysis_data
                    FROM news_article_analysis
                    WHERE article_id::text = ANY($1)
                    """,
                    ids,
                )
                for r in narows:
                    analyses[r["article_id"]] = _build_item_analysis(r)
        except Exception as e:
            logger.warning(f"batch_items analysis resolution failed (non-fatal): {e}")

        def _analysis_for(resource_type: str, resource_id: str):
            return analyses.get(resource_id) if resource_type == "article" else None

        items = []
        for r in rows:
            key = (r["resource_type"], str(r["resource_id"]))
            items.append(MinerBatchItem(
                resource_type=r["resource_type"],
                resource_id=str(r["resource_id"]),
                content_preview=previews.get(key) or r.get("post_preview"),
                cause=r["cause"],
                failed_fields=_as_json(r.get("failed_fields")),
                miner_values=_as_json(r.get("miner_values")),
                validator_values=_as_json(r.get("validator_values")),
                post_preview=r.get("post_preview"),
                score=(float(r["score"]) if r.get("score") is not None else None),
                validator_hotkey=r.get("validator_hotkey"),
                analysis=_analysis_for(r["resource_type"], str(r["resource_id"])),
            ))

        earned = []
        for r in earned_norm:
            key = (r["resource_type"], str(r["resource_id"]))
            earned.append(EarnedItem(
                resource_type=r["resource_type"],
                resource_id=str(r["resource_id"]),
                content_preview=previews.get(key),
                points_awarded=float(r.get("points_awarded") or 0.0),
                categorical_key=r.get("categorical_key"),
                validator_hotkey=r.get("validator_hotkey"),
                analysis=_analysis_for(r["resource_type"], str(r["resource_id"])),
            ))

        start, stop = _block_window(int(epoch))
        return MinerBatchItemsResponse(
            hotkey=hotkey, epoch=int(epoch),
            block_window_start=start, block_window_stop=stop,
            items=items, earned=earned,
            earned_truncated=(len(earned_rows) >= EARNED_LIMIT),
        )
    except Exception as e:
        logger.error(f"Error in dashboard_miner_batch_items: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/miners/{hotkey}/reputation", response_model=MinerReputationResponse)
async def dashboard_miner_reputation(hotkey: str):
    """Per-epoch reputation history for a miner (display-only, decoupled from consensus).
    Reads the append-only reputation_snapshot table. The 'gate' column already holds the
    emission multiplier (~0 below the cliff, ~1, up to ~1.3 with the bonus); surfaced as
    emission_mult. Dedups to one row per epoch (most recent write) for a clean series, and
    includes the served gate/bonus params so the UI can draw the cliff + bonus band."""
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
            SELECT DISTINCT ON (epoch)
                   epoch, reputation, samples, gate, validator_hotkey, created_at
            FROM reputation_snapshot
            WHERE miner_hotkey = $1
            ORDER BY epoch DESC, created_at DESC
            LIMIT 200
            """,
            hotkey,
        )
        history = [
            ReputationPoint(
                epoch=int(r["epoch"]),
                reputation=float(r["reputation"]),
                samples=int(r["samples"] or 0),
                emission_mult=(float(r["gate"]) if r["gate"] is not None else None),
                validator_hotkey=r["validator_hotkey"],
                created_at=r.get("created_at"),
            )
            for r in rows
        ]
        return MinerReputationResponse(
            hotkey=hotkey,
            emission_midpoint=float(os.getenv("SUBNET_EMISSION_MIDPOINT", "0.59")),
            bonus_start=float(os.getenv("SUBNET_EMISSION_BONUS_START", "0.63")),
            bonus_full=float(os.getenv("SUBNET_EMISSION_BONUS_FULL", "0.75")),
            bonus_ceiling=float(os.getenv("SUBNET_EMISSION_BONUS_CEILING", "0.0")),
            latest=(history[0] if history else None),
            history=history,
        )
    except Exception as e:
        logger.error(f"Error in dashboard_miner_reputation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/miners/{hotkey}/events", response_model=MinerEventsResponse)
async def dashboard_miner_events(hotkey: str, limit: int = 100):
    """Per-miner dispatch/cooldown event log (display-only, decoupled from consensus).
    Reads the append-only miner_event table, newest first — park/unpark, batch-size
    changes, reward-zeroed, so a miner can see when and why their dispatch state changed."""
    prisma = _get_prisma()
    try:
        limit = max(1, min(int(limit), 500))
        rows = await prisma.query_raw(
            """
            SELECT event_type, occurred_at, streak, shadow, epoch, reason, detail, validator_hotkey
            FROM miner_event
            WHERE miner_hotkey = $1
            ORDER BY occurred_at DESC
            LIMIT $2
            """,
            hotkey,
            limit,
        )
        events = [
            MinerEventRow(
                event_type=r["event_type"],
                occurred_at=r["occurred_at"],
                streak=r.get("streak"),
                shadow=r.get("shadow"),
                epoch=(int(r["epoch"]) if r.get("epoch") is not None else None),
                reason=r.get("reason"),
                detail=_as_json(r.get("detail")),
                validator_hotkey=r["validator_hotkey"],
            )
            for r in rows
        ]
        return MinerEventsResponse(hotkey=hotkey, events=events)
    except Exception as e:
        logger.error(f"Error in dashboard_miner_events: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Asset Coverage Endpoint
# ============================================================================

@router.get("/assets", response_model=AssetCoverageResponse)
async def dashboard_assets():
    """Asset coverage with sentiment breakdown across all source types."""
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
            SELECT
                asset_symbol,
                COUNT(*) AS total_items,
                SUM(CASE WHEN source = 'tweet' THEN 1 ELSE 0 END) AS tweet_count,
                SUM(CASE WHEN source = 'telegram' THEN 1 ELSE 0 END) AS telegram_count,
                SUM(CASE WHEN source = 'article' THEN 1 ELSE 0 END) AS article_count,
                SUM(CASE WHEN sentiment = 'very_bullish' THEN 1 ELSE 0 END) AS very_bullish,
                SUM(CASE WHEN sentiment = 'bullish' THEN 1 ELSE 0 END) AS bullish,
                SUM(CASE WHEN sentiment = 'neutral' THEN 1 ELSE 0 END) AS neutral,
                SUM(CASE WHEN sentiment = 'bearish' THEN 1 ELSE 0 END) AS bearish,
                SUM(CASE WHEN sentiment = 'very_bearish' THEN 1 ELSE 0 END) AS very_bearish,
                MIN(analyzed_at) AS first_seen,
                MAX(analyzed_at) AS last_seen
            FROM (
                SELECT asset_symbol, sentiment, analyzed_at, 'tweet' AS source
                FROM tweet_analysis WHERE asset_symbol IS NOT NULL AND asset_symbol != 'NONE'
                UNION ALL
                SELECT asset_symbol, sentiment, analyzed_at, 'telegram' AS source
                FROM telegram_message_analysis WHERE asset_symbol IS NOT NULL AND asset_symbol != 'NONE'
                UNION ALL
                SELECT sector_symbol AS asset_symbol, sentiment, analyzed_at, 'article' AS source
                FROM news_article_analysis WHERE sector_symbol IS NOT NULL AND sector_symbol != 'OTHER'
            ) AS all_assets
            GROUP BY asset_symbol
            ORDER BY total_items DESC
            """
        )

        assets = []
        for row in rows:
            assets.append(AssetEntry(
                asset_symbol=row["asset_symbol"],
                total_items=row["total_items"],
                tweet_count=row["tweet_count"],
                telegram_count=row["telegram_count"],
                article_count=row["article_count"],
                sentiment=SentimentDistribution(
                    very_bullish=row["very_bullish"],
                    bullish=row["bullish"],
                    neutral=row["neutral"],
                    bearish=row["bearish"],
                    very_bearish=row["very_bearish"],
                ),
                first_seen=row.get("first_seen"),
                last_seen=row.get("last_seen"),
            ))

        return AssetCoverageResponse(assets=assets, total=len(assets))
    except Exception as e:
        logger.error(f"Error in dashboard_assets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Validator Activity Endpoint
# ============================================================================

@router.get("/validators", response_model=ValidatorActivityResponse)
async def dashboard_validators():
    """Validator activity stats across all source types.

    Counts are de-duplicated to distinct items. An item re-scored across epochs,
    or scored by multiple validators, is counted once. Per-validator counts are
    the distinct items that validator scored; the response-level
    ``total_distinct_scored`` is the distinct item count across validators active
    in the last 24h — a plain sum of per-validator counts would double-count
    items scored by more than one validator.

    Distinct items are counted per source table on the indexed integer id, which
    is far cheaper than a DISTINCT over a unioned (source, id) key.
    """
    prisma = _get_prisma()
    try:
        per_source = (
            ("tweet", "scoring", "tweet_id"),
            ("telegram", "telegram_scoring", "message_id"),
            ("article", "news_article_scoring", "article_id"),
        )

        # validator_hotkey -> aggregated per-source distinct stats
        agg: dict[str, dict] = {}
        for source, table, id_col in per_source:
            src_rows = await prisma.query_raw(
                f"""
                SELECT
                    validator_hotkey,
                    COUNT(DISTINCT {id_col}) AS items,
                    COUNT(DISTINCT {id_col}) FILTER (WHERE status = 'completed') AS completed,
                    MIN(created_at) AS first_seen,
                    MAX(created_at) AS last_seen
                FROM {table}
                WHERE validator_hotkey IS NOT NULL
                GROUP BY validator_hotkey
                """
            )
            for row in src_rows:
                hk = row["validator_hotkey"]
                v = agg.setdefault(hk, {
                    "tweet": 0, "telegram": 0, "article": 0,
                    "completed": 0, "first_seen": None, "last_seen": None,
                })
                v[source] = row["items"] or 0
                v["completed"] += row["completed"] or 0
                fs, ls = row.get("first_seen"), row.get("last_seen")
                if fs and (v["first_seen"] is None or fs < v["first_seen"]):
                    v["first_seen"] = fs
                if ls and (v["last_seen"] is None or ls > v["last_seen"]):
                    v["last_seen"] = ls

        validators = []
        for hk, v in agg.items():
            validators.append(ValidatorEntry(
                hotkey=hk,
                total_scored=v["tweet"] + v["telegram"] + v["article"],
                tweet_count=v["tweet"],
                telegram_count=v["telegram"],
                article_count=v["article"],
                completed_count=v["completed"],
                first_seen=v["first_seen"],
                last_seen=v["last_seen"],
            ))
        validators.sort(key=lambda e: e.total_scored, reverse=True)

        # Header figure: distinct items scored across validators active in the
        # last 24h. Computed globally (not summed from the rows above) so an item
        # scored by multiple active validators is counted once.
        gdist_rows = await prisma.query_raw(
            """
            WITH active AS (
                SELECT validator_hotkey FROM (
                    SELECT validator_hotkey, created_at FROM scoring WHERE validator_hotkey IS NOT NULL
                    UNION ALL
                    SELECT validator_hotkey, created_at FROM telegram_scoring WHERE validator_hotkey IS NOT NULL
                    UNION ALL
                    SELECT validator_hotkey, created_at FROM news_article_scoring WHERE validator_hotkey IS NOT NULL
                ) u
                GROUP BY validator_hotkey
                HAVING MAX(created_at) >= now() - interval '24 hours'
            )
            SELECT
                (SELECT COUNT(DISTINCT tweet_id) FROM scoring
                   WHERE validator_hotkey IN (SELECT validator_hotkey FROM active)) AS tw,
                (SELECT COUNT(DISTINCT message_id) FROM telegram_scoring
                   WHERE validator_hotkey IN (SELECT validator_hotkey FROM active)) AS tg,
                (SELECT COUNT(DISTINCT article_id) FROM news_article_scoring
                   WHERE validator_hotkey IN (SELECT validator_hotkey FROM active)) AS ar
            """
        )
        g = gdist_rows[0] if gdist_rows else {}
        total_distinct_scored = (g.get("tw") or 0) + (g.get("tg") or 0) + (g.get("ar") or 0)

        return ValidatorActivityResponse(
            validators=validators,
            total=len(validators),
            total_distinct_scored=total_distinct_scored,
        )
    except Exception as e:
        logger.error(f"Error in dashboard_validators: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Event Endpoints
# ============================================================================


@router.get("/events", response_model=EventsResponse)
async def dashboard_events(
    days: int = 7,
    sector: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
    page: int = 1,
    _ip=Depends(_require_local),
):
    """Get recent canonical events with article counts and sentiment."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    where: dict = {"lastArticleAt": {"gte": cutoff}}
    if sector:
        where["sectorSymbol"] = sector.upper()
    if event_type:
        where["eventType"] = event_type

    total = await prisma.event.count(where=where)
    events = await prisma.event.find_many(
        where=where,
        order_by={"lastArticleAt": "desc"},
        skip=(page - 1) * limit,
        take=limit,
    )

    return EventsResponse(
        events=[
            EventSummary(
                id=e.id,
                event_type=e.eventType,
                canonical_title=e.canonicalTitle,
                event_date=e.eventDate.isoformat() if e.eventDate else None,
                sector_id=e.sectorId,
                sector_symbol=e.sectorSymbol,
                article_count=e.articleCount,
                sentiment=e.sentiment,
                impact_potential=e.impactPotential,
                summary=e.summary,
                first_seen_at=e.firstSeenAt,
                last_article_at=e.lastArticleAt,
            )
            for e in events
        ],
        total=total,
    )


@router.get("/events/trending", response_model=EventsResponse)
async def dashboard_events_trending(
    limit: int = 20,
    _ip=Depends(_require_local),
):
    """Get trending events ranked by article count in last 24 hours."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    events = await prisma.event.find_many(
        where={"lastArticleAt": {"gte": cutoff}},
        order_by={"articleCount": "desc"},
        take=limit,
    )

    return EventsResponse(
        events=[
            EventSummary(
                id=e.id, event_type=e.eventType, canonical_title=e.canonicalTitle,
                event_date=e.eventDate.isoformat() if e.eventDate else None,
                sector_id=e.sectorId, sector_symbol=e.sectorSymbol,
                article_count=e.articleCount, sentiment=e.sentiment,
                impact_potential=e.impactPotential, summary=e.summary,
                first_seen_at=e.firstSeenAt, last_article_at=e.lastArticleAt,
            )
            for e in events
        ],
        total=len(events),
    )


@router.get("/events/{event_id}", response_model=EventDetailResponse)
async def dashboard_event_detail(
    event_id: int,
    _ip=Depends(_require_local),
):
    """Get full event detail with linked articles."""
    event = await prisma.event.find_unique(where={"id": event_id})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    links = await prisma.eventarticle.find_many(
        where={"eventId": event_id},
        include={"article": True},
        order_by={"addedAt": "desc"},
        take=100,
    )

    articles = []
    for link in links:
        if link.article:
            articles.append({
                "id": link.article.id,
                "title": link.article.title,
                "url": link.article.url,
                "source": link.article.source,
                "published": link.article.published.isoformat() if link.article.published else None,
                "role": link.role,
            })

    return EventDetailResponse(
        event=EventSummary(
            id=event.id, event_type=event.eventType, canonical_title=event.canonicalTitle,
            event_date=event.eventDate.isoformat() if event.eventDate else None,
            sector_id=event.sectorId, sector_symbol=event.sectorSymbol,
            article_count=event.articleCount, sentiment=event.sentiment,
            impact_potential=event.impactPotential, summary=event.summary,
            first_seen_at=event.firstSeenAt, last_article_at=event.lastArticleAt,
        ),
        articles=articles,
        entities=event.entities if isinstance(event.entities, list) else None,
    )


# ============================================================================
# Narrative Endpoints
# ============================================================================


@router.get("/narratives", response_model=NarrativesResponse)
async def dashboard_narratives(
    phase: Optional[str] = None,
    sector: Optional[int] = None,
    limit: int = 50,
    _ip=Depends(_require_local),
):
    """Get market narratives with metadata."""
    where: dict = {}
    if phase:
        where["phase"] = phase
    else:
        where["phase"] = {"in": ["active", "emerging", "peak"]}

    narratives = await prisma.narrative.find_many(
        where=where,
        order_by={"articleCount": "desc"},
        take=limit,
    )

    return NarrativesResponse(
        narratives=[
            NarrativeSummary(
                id=n.id, slug=n.slug, name=n.name, description=n.description,
                phase=n.phase, article_count=n.articleCount, event_count=n.eventCount,
                sentiment_score=n.sentimentScore, momentum=n.momentum,
                source=n.source, last_article_at=n.lastArticleAt,
            )
            for n in narratives
        ],
        total=len(narratives),
    )


@router.get("/narratives/trending", response_model=NarrativesResponse)
async def dashboard_narratives_trending(
    limit: int = 20,
    _ip=Depends(_require_local),
):
    """Get narratives with most recent activity."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    narratives = await prisma.narrative.find_many(
        where={"lastArticleAt": {"gte": cutoff}, "phase": {"in": ["active", "emerging", "peak"]}},
        order_by={"articleCount": "desc"},
        take=limit,
    )

    return NarrativesResponse(
        narratives=[
            NarrativeSummary(
                id=n.id, slug=n.slug, name=n.name, description=n.description,
                phase=n.phase, article_count=n.articleCount, event_count=n.eventCount,
                sentiment_score=n.sentimentScore, momentum=n.momentum,
                source=n.source, last_article_at=n.lastArticleAt,
            )
            for n in narratives
        ],
        total=len(narratives),
    )
