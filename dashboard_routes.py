"""
Dashboard API routes — read-only endpoints restricted to local / allowed IPs.

Mounted on the main FastAPI app via `app.include_router(router)`.
"""

import os
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
    MinerLeaderboardEntry,
    MinerLeaderboardResponse,
    MinerRecentItem,
    MinerRewardEntry,
    MinerPenaltyEntry,
    MinerProfileResponse,
    AssetEntry,
    AssetCoverageResponse,
    ValidatorEntry,
    ValidatorActivityResponse,
)

logger = logging.getLogger(__name__)

DASHBOARD_ALLOWED_IPS: set[str] = {"127.0.0.1", "::1"}
DASHBOARD_ALLOWED_IPS.update(
    ip.strip()
    for ip in os.getenv("DASHBOARD_ALLOWED_IPS", "").split(",")
    if ip.strip()
)


async def _require_local(request: Request):
    client_ip = request.client.host if request.client else None
    if client_ip not in DASHBOARD_ALLOWED_IPS:
        raise HTTPException(status_code=403, detail="Dashboard access restricted.")


router = APIRouter(prefix="/dashboard", dependencies=[Depends(_require_local)])


def _get_prisma():
    from main import prisma
    return prisma


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


@router.get("/feed", response_model=FeedResponse)
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

        sources = [
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


@router.get("/articles", response_model=ArticlesResponse)
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

        base_query = f"""
            FROM news_articles na
            LEFT JOIN news_article_analysis naa ON naa.article_id = na.id
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


@router.get("/articles/{article_id}", response_model=ArticleDetailResponse)
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


@router.get("/tweets/{tweet_id}", response_model=TweetDetailResponse)
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


@router.get("/telegram/{message_id}", response_model=TelegramDetailResponse)
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

@router.get("/miners", response_model=MinerLeaderboardResponse)
async def dashboard_miners(
    sort_by: Optional[str] = "total_items",
    limit: int = 50,
):
    """Miner leaderboard with aggregated stats across all source types."""
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
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
                    MIN(analyzed_at) AS first_seen,
                    MAX(analyzed_at) AS last_seen
                FROM (
                    SELECT miner_hotkey, sentiment, analyzed_at, 'tweet' AS source
                    FROM tweet_analysis WHERE miner_hotkey IS NOT NULL
                    UNION ALL
                    SELECT miner_hotkey, sentiment, analyzed_at, 'telegram' AS source
                    FROM telegram_message_analysis WHERE miner_hotkey IS NOT NULL
                    UNION ALL
                    SELECT miner_hotkey, sentiment, analyzed_at, 'article' AS source
                    FROM news_article_analysis WHERE miner_hotkey IS NOT NULL
                ) AS all_analyses
                GROUP BY miner_hotkey
            )
            SELECT
                ms.*,
                COALESCE(r.total_points, 0) AS total_rewards
            FROM miner_stats ms
            LEFT JOIN (
                SELECT hotkey, SUM(points) AS total_points
                FROM rewards
                GROUP BY hotkey
            ) r ON r.hotkey = ms.miner_hotkey
            ORDER BY
                CASE WHEN $1 = 'total_rewards' THEN COALESCE(r.total_points, 0) ELSE 0 END DESC,
                CASE WHEN $1 = 'recent' THEN EXTRACT(EPOCH FROM ms.last_seen) ELSE 0 END DESC,
                ms.total_items DESC
            LIMIT $2
            """,
            sort_by,
            limit,
        )

        miners = []
        for row in rows:
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
            """
            (
                SELECT 'tweet' AS source_type, ta.tweet_id::text AS id,
                    t.text AS content, ta.sentiment, ta.asset_symbol,
                    ta.impact_potential, ta.technical_quality, ta.analyzed_at
                FROM tweet_analysis ta
                JOIN tweets t ON t.id = ta.tweet_id
                WHERE ta.miner_hotkey = $1
                ORDER BY ta.analyzed_at DESC LIMIT 20
            )
            UNION ALL
            (
                SELECT 'telegram' AS source_type, tma.message_id AS id,
                    tm.content, tma.sentiment, tma.asset_symbol,
                    tma.impact_potential, tma.technical_quality, tma.analyzed_at
                FROM telegram_message_analysis tma
                JOIN telegram_messages tm ON tm.id = tma.message_id
                WHERE tma.miner_hotkey = $1
                ORDER BY tma.analyzed_at DESC LIMIT 20
            )
            UNION ALL
            (
                SELECT 'article' AS source_type, naa.article_id::text AS id,
                    na.title AS content, naa.sentiment, naa.sector_symbol AS asset_symbol,
                    naa.impact_potential, naa.technical_quality, naa.analyzed_at
                FROM news_article_analysis naa
                JOIN news_articles na ON na.id = naa.article_id
                WHERE naa.miner_hotkey = $1
                ORDER BY naa.analyzed_at DESC LIMIT 20
            )
            ORDER BY analyzed_at DESC
            LIMIT 50
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
    """Validator activity stats across all source types."""
    prisma = _get_prisma()
    try:
        rows = await prisma.query_raw(
            """
            SELECT
                validator_hotkey,
                COUNT(*) AS total_scored,
                SUM(CASE WHEN source = 'tweet' THEN 1 ELSE 0 END) AS tweet_count,
                SUM(CASE WHEN source = 'telegram' THEN 1 ELSE 0 END) AS telegram_count,
                SUM(CASE WHEN source = 'article' THEN 1 ELSE 0 END) AS article_count,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                MIN(created_at) AS first_seen,
                MAX(created_at) AS last_seen
            FROM (
                SELECT validator_hotkey, status, created_at, 'tweet' AS source
                FROM scoring WHERE validator_hotkey IS NOT NULL
                UNION ALL
                SELECT validator_hotkey, status, created_at, 'telegram' AS source
                FROM telegram_scoring WHERE validator_hotkey IS NOT NULL
                UNION ALL
                SELECT validator_hotkey, status, created_at, 'article' AS source
                FROM news_article_scoring WHERE validator_hotkey IS NOT NULL
            ) AS all_scoring
            GROUP BY validator_hotkey
            ORDER BY total_scored DESC
            """
        )

        validators = []
        for row in rows:
            validators.append(ValidatorEntry(
                hotkey=row["validator_hotkey"],
                total_scored=row["total_scored"],
                tweet_count=row["tweet_count"],
                telegram_count=row["telegram_count"],
                article_count=row["article_count"],
                completed_count=row["completed_count"],
                first_seen=row.get("first_seen"),
                last_seen=row.get("last_seen"),
            ))

        return ValidatorActivityResponse(validators=validators, total=len(validators))
    except Exception as e:
        logger.error(f"Error in dashboard_validators: {e}")
        raise HTTPException(status_code=500, detail=str(e))

