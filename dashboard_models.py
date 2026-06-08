"""
Pydantic models for the public dashboard API endpoints.

These are read-only response models for the dashboard frontend.
"""

from datetime import datetime
from typing import Optional, List, Any, Dict
from pydantic import BaseModel


# ============================================================================
# Stats Endpoint Models
# ============================================================================

class SourceCounts(BaseModel):
    """Total and analyzed counts for a single source type."""
    total: int = 0
    analyzed: int = 0
    analyzed_today: int = 0


class SentimentDistribution(BaseModel):
    """Sentiment counts across all sources."""
    very_bullish: int = 0
    bullish: int = 0
    neutral: int = 0
    bearish: int = 0
    very_bearish: int = 0


class DashboardStatsResponse(BaseModel):
    """Response for GET /dashboard/stats."""
    tweets: SourceCounts
    telegram: SourceCounts
    articles: SourceCounts
    latest_analysis_at: Optional[datetime] = None
    sentiment: SentimentDistribution


# ============================================================================
# Feed Endpoint Models
# ============================================================================

class FeedItemAuthor(BaseModel):
    """Author metadata for tweet feed items."""
    screen_name: Optional[str] = None
    profile_image_url: Optional[str] = None


class FeedItemArticleMeta(BaseModel):
    """Article-specific metadata for feed items."""
    title: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    sector_symbol: Optional[str] = None


class FeedItemTelegramMeta(BaseModel):
    """Telegram-specific metadata for feed items."""
    sender_username: Optional[str] = None
    sender_name: Optional[str] = None
    group_title: Optional[str] = None


class FeedItem(BaseModel):
    """A single item in the unified feed."""
    source_type: str  # "tweet", "telegram", "article"
    id: Optional[str] = None
    content: Optional[str] = None
    sentiment: Optional[str] = None
    asset_symbol: Optional[str] = None
    content_type: Optional[str] = None
    impact_potential: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    timestamp: Optional[datetime] = None

    # Source-specific metadata (only one will be populated)
    author: Optional[FeedItemAuthor] = None
    telegram: Optional[FeedItemTelegramMeta] = None
    article: Optional[FeedItemArticleMeta] = None


class FeedResponse(BaseModel):
    """Response for GET /dashboard/feed."""
    items: List[FeedItem]
    total: int
    page: int
    limit: int
    has_more: bool


# ============================================================================
# Articles Endpoint Models
# ============================================================================

class ArticleWithAnalysis(BaseModel):
    """An article with its analysis data joined."""
    id: int
    url: str
    title: str
    summary: Optional[str] = None
    content: Optional[str] = None
    published: Optional[datetime] = None
    source: str
    topic: Optional[str] = None
    created_at: Optional[datetime] = None
    # Analysis fields
    sentiment: Optional[str] = None
    sector_symbol: Optional[str] = None
    content_type: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    impact_potential: Optional[str] = None
    relevance_confidence: Optional[str] = None
    mentioned_assets: Optional[Any] = None
    analyzed_at: Optional[datetime] = None
    miner_hotkey: Optional[str] = None


class ArticlesResponse(BaseModel):
    """Response for GET /dashboard/articles."""
    articles: List[ArticleWithAnalysis]
    total: int
    page: int
    limit: int
    has_more: bool


class ArticleDetailResponse(ArticleWithAnalysis):
    """Response for GET /dashboard/articles/{article_id}. Same as ArticleWithAnalysis."""
    pass


class SourceStats(BaseModel):
    """Aggregated stats for a single news source."""
    source: str
    total_articles: int = 0
    analyzed_articles: int = 0
    very_bullish: int = 0
    bullish: int = 0
    neutral: int = 0
    bearish: int = 0
    very_bearish: int = 0


class ArticleSourcesResponse(BaseModel):
    """Response for GET /dashboard/articles/sources."""
    sources: List[SourceStats]


# ============================================================================
# Tweet Detail Model
# ============================================================================

class TweetDetailResponse(BaseModel):
    """Response for GET /dashboard/tweets/{tweet_id}."""
    id: str
    text: Optional[str] = None
    url: Optional[str] = None
    lang: Optional[str] = None
    retweet_count: int = 0
    reply_count: int = 0
    like_count: int = 0
    quote_count: int = 0
    view_count: int = 0
    bookmark_count: int = 0
    is_reply: bool = False
    created_at: Optional[datetime] = None
    # Author
    author_screen_name: Optional[str] = None
    author_name: Optional[str] = None
    author_profile_image_url: Optional[str] = None
    author_followers_count: int = 0
    author_verified: bool = False
    # Analysis
    sentiment: Optional[str] = None
    asset_id: Optional[int] = None
    asset_symbol: Optional[str] = None
    content_type: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    impact_potential: Optional[str] = None
    relevance_confidence: Optional[str] = None
    analyzed_at: Optional[datetime] = None
    miner_hotkey: Optional[str] = None


# ============================================================================
# Telegram Detail Model
# ============================================================================

class TelegramDetailResponse(BaseModel):
    """Response for GET /dashboard/telegram/{message_id}."""
    id: str
    telegram_id: Optional[str] = None
    content: Optional[str] = None
    sender_username: Optional[str] = None
    sender_name: Optional[str] = None
    group_title: Optional[str] = None
    group_telegram_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    created_at: Optional[datetime] = None
    # Analysis
    sentiment: Optional[str] = None
    asset_id: Optional[int] = None
    asset_symbol: Optional[str] = None
    content_type: Optional[str] = None
    technical_quality: Optional[str] = None
    market_analysis: Optional[str] = None
    impact_potential: Optional[str] = None
    relevance_confidence: Optional[str] = None
    analyzed_at: Optional[datetime] = None
    miner_hotkey: Optional[str] = None


# ============================================================================
# Sentiment Endpoint Models
# ============================================================================

class DailySentiment(BaseModel):
    """Sentiment counts for a single day."""
    date: str  # YYYY-MM-DD
    very_bullish: int = 0
    bullish: int = 0
    neutral: int = 0
    bearish: int = 0
    very_bearish: int = 0


class SentimentResponse(BaseModel):
    """Response for GET /dashboard/sentiment."""
    overall: SentimentDistribution
    daily: List[DailySentiment]
    days: int
    source_type: Optional[str] = None
    asset: Optional[str] = None


# ============================================================================
# Miner Leaderboard & Profile Models
# ============================================================================

class MinerLeaderboardEntry(BaseModel):
    """A single miner's aggregated stats for the leaderboard."""
    hotkey: str
    total_items: int = 0
    tweet_count: int = 0
    telegram_count: int = 0
    article_count: int = 0
    sentiment: SentimentDistribution
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    total_rewards: float = 0.0


class MinerLeaderboardResponse(BaseModel):
    """Response for GET /dashboard/miners."""
    miners: List[MinerLeaderboardEntry]
    total: int


class MinerRecentItem(BaseModel):
    """A single item from a miner's work history."""
    source_type: str
    id: str
    content: Optional[str] = None
    sentiment: Optional[str] = None
    asset_symbol: Optional[str] = None
    impact_potential: Optional[str] = None
    technical_quality: Optional[str] = None
    analyzed_at: Optional[datetime] = None


class MinerRewardEntry(BaseModel):
    """A single reward record for a miner."""
    start_block: int
    stop_block: int
    points: float
    created_at: Optional[datetime] = None


class MinerPenaltyEntry(BaseModel):
    """A single penalty record for a miner."""
    reason: str
    timestamp: Optional[datetime] = None


class MinerProfileResponse(BaseModel):
    """Response for GET /dashboard/miners/{hotkey}."""
    hotkey: str
    total_items: int = 0
    tweet_count: int = 0
    telegram_count: int = 0
    article_count: int = 0
    sentiment: SentimentDistribution
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    total_rewards: float = 0.0
    total_penalties: int = 0
    recent_items: List[MinerRecentItem] = []
    rewards: List[MinerRewardEntry] = []
    penalties: List[MinerPenaltyEntry] = []


# ============================================================================
# Miner Batch / Scoring-Transparency Models
# ============================================================================
# Built on the standalone `penalty_detail` table + the `score_verdict` valid trail.
# DECOUPLED from consensus: read-only, never writes, never touches attestation/Merkle.

class Diagnosis(BaseModel):
    """Plain-language diagnosis for a batch (server-side so it's consistent everywhere)."""
    severity: str                       # ok | warn | error
    headline: str
    detail: Optional[str] = None
    action: Optional[str] = None


class MinerBatch(BaseModel):
    """One epoch / block-window of a miner's scoring, as a statement line item."""
    epoch: int
    block_window_start: int
    block_window_stop: int
    last_activity_at: Optional[datetime] = None   # max(created_at) across the two trails (for "2h ago")
    # Distinct valid items (pre-zeroing). Equals raw points while verdicts are uniformly
    # 1.0 (true today); switch to summing points-per-item if valid-side gradation lands.
    earned_items: int = 0
    penalty_items: int = 0              # distinct items penalized (from penalty_detail)
    penalty_breakdown: Dict[str, int] = {}   # cause -> distinct items penalized
    # Best reward-table points for this epoch: MAX(points) across the per-validator rows
    # (the rewards table has no validator column; MAX is the conservative "most generous"
    # view). POST-zeroing. This is reward-table value, NOT the chain incentive/emission.
    # None => no reward row written yet for this epoch (on-chain outcome pending).
    reward_points_max: Optional[float] = None
    # True only when the miner did valid work AND every reward row for the epoch is 0
    # (consensus zeroed it). False while reward_points_max is None (pending), so the UI
    # never shows "zeroed" before the reward row lands.
    was_zeroed: bool = False
    # Distinct validators that wrote per-item DETAIL this epoch (a subset of all validators
    # that flagged the miner). Attribution copy only -- NOT the zeroing decision (use was_zeroed).
    flagged_by_validators: int = 0
    detail_coverage: bool = False       # whether per-item attribution exists for this epoch
    diagnosis: Diagnosis


class MinerBatchesResponse(BaseModel):
    """Response for GET /dashboard/miners/{hotkey}/batches."""
    hotkey: str
    block_length: int
    batches: List[MinerBatch] = []


class MinerBatchItem(BaseModel):
    """A single penalized item within a batch — the miner-vs-validator diff."""
    resource_type: str
    resource_id: str
    content_preview: Optional[str] = None
    cause: str
    failed_fields: Optional[List[str]] = None
    miner_values: Optional[Dict[str, Any]] = None
    validator_values: Optional[Dict[str, Any]] = None
    post_preview: Optional[str] = None
    validator_hotkey: Optional[str] = None


class EarnedItem(BaseModel):
    """A single item the validator selected and scored VALID for this miner+epoch.
    No miner-vs-validator diff (it matched); render as a 'selected and scored valid' card."""
    resource_type: str
    resource_id: str
    content_preview: Optional[str] = None
    points_awarded: float = 0.0
    categorical_key: Optional[str] = None   # validator's categorization; optional to render
    validator_hotkey: Optional[str] = None


class MinerBatchItemsResponse(BaseModel):
    """Response for GET /dashboard/miners/{hotkey}/batches/{epoch}/items."""
    hotkey: str
    epoch: int
    block_window_start: int
    block_window_stop: int
    items: List[MinerBatchItem] = []        # penalized items (diffs) — unchanged contract
    earned: List[EarnedItem] = []           # valid items the validator scored this epoch
    earned_truncated: bool = False          # true if `earned` hit the row cap (not the full set)


# ============================================================================
# Asset Coverage Models
# ============================================================================

class AssetEntry(BaseModel):
    """Aggregated stats for a single asset."""
    asset_symbol: str
    total_items: int = 0
    tweet_count: int = 0
    telegram_count: int = 0
    article_count: int = 0
    sentiment: SentimentDistribution
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None


class AssetCoverageResponse(BaseModel):
    """Response for GET /dashboard/assets."""
    assets: List[AssetEntry]
    total: int


# ============================================================================
# Validator Activity Models
# ============================================================================

class ValidatorEntry(BaseModel):
    """Aggregated stats for a single validator."""
    hotkey: str
    total_scored: int = 0
    tweet_count: int = 0
    telegram_count: int = 0
    article_count: int = 0
    completed_count: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None


class ValidatorActivityResponse(BaseModel):
    """Response for GET /dashboard/validators."""
    validators: List[ValidatorEntry]
    total: int
