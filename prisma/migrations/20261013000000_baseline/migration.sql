-- CreateTable
CREATE TABLE "accounts" (
    "id" BIGINT NOT NULL,
    "name" TEXT,
    "screen_name" TEXT NOT NULL,
    "user_name" TEXT,
    "location" TEXT,
    "url" TEXT,
    "description" TEXT,
    "email" TEXT,
    "protected" BOOLEAN NOT NULL DEFAULT false,
    "verified" BOOLEAN NOT NULL DEFAULT false,
    "is_blue_verified" BOOLEAN NOT NULL DEFAULT false,
    "followers_count" INTEGER NOT NULL DEFAULT 0,
    "following_count" INTEGER NOT NULL DEFAULT 0,
    "friends_count" INTEGER NOT NULL DEFAULT 0,
    "favourites_count" INTEGER NOT NULL DEFAULT 0,
    "statuses_count" INTEGER NOT NULL DEFAULT 0,
    "media_count" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ,
    "profile_banner_url" TEXT,
    "profile_image_url" TEXT,
    "can_dm" BOOLEAN NOT NULL DEFAULT false,
    "can_media_tag" BOOLEAN NOT NULL DEFAULT true,
    "is_translator" BOOLEAN NOT NULL DEFAULT false,
    "is_automated" BOOLEAN NOT NULL DEFAULT false,
    "automated_by" TEXT,
    "possibly_sensitive" BOOLEAN NOT NULL DEFAULT false,
    "first_seen_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "accounts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "monitored_accounts" (
    "id" SERIAL NOT NULL,
    "screen_name" TEXT NOT NULL,
    "account_id" BIGINT,
    "rule_id" TEXT,
    "rule_tag" TEXT,
    "is_active" BOOLEAN NOT NULL DEFAULT true,
    "added_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_tweet_at" TIMESTAMPTZ,

    CONSTRAINT "monitored_accounts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "source_accounts" (
    "id" SERIAL NOT NULL,
    "screen_name" TEXT NOT NULL,
    "processed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_accounts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "relationships" (
    "id" SERIAL NOT NULL,
    "source_account" TEXT NOT NULL,
    "target_account_id" BIGINT NOT NULL,
    "relationship_type" TEXT NOT NULL,
    "discovered_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "relationships_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweets" (
    "id" BIGINT NOT NULL,
    "type" TEXT NOT NULL DEFAULT 'tweet',
    "url" TEXT,
    "text" TEXT,
    "source" TEXT,
    "lang" TEXT,
    "retweet_count" INTEGER NOT NULL DEFAULT 0,
    "reply_count" INTEGER NOT NULL DEFAULT 0,
    "like_count" INTEGER NOT NULL DEFAULT 0,
    "quote_count" INTEGER NOT NULL DEFAULT 0,
    "view_count" INTEGER NOT NULL DEFAULT 0,
    "bookmark_count" INTEGER NOT NULL DEFAULT 0,
    "is_reply" BOOLEAN NOT NULL DEFAULT false,
    "in_reply_to_id" BIGINT,
    "conversation_id" BIGINT,
    "in_reply_to_user_id" BIGINT,
    "in_reply_to_username" TEXT,
    "author_id" BIGINT,
    "quoted_tweet_id" BIGINT,
    "retweeted_tweet_id" BIGINT,
    "rule_id" TEXT,
    "rule_tag" TEXT,
    "card_data" JSONB,
    "place_data" JSONB,
    "created_at" TIMESTAMPTZ,
    "received_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "tweets_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweet_analysis" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "sentiment" TEXT,
    "subnet_id" INTEGER,
    "subnet_name" TEXT,
    "asset_id" INTEGER,
    "asset_symbol" TEXT,
    "content_type" TEXT,
    "technical_quality" TEXT,
    "market_analysis" TEXT,
    "impact_potential" TEXT,
    "relevance_confidence" TEXT,
    "analysis_data" JSONB,
    "miner_hotkey" TEXT,
    "analyzed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "tweet_analysis_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweet_media" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "media_id" TEXT NOT NULL,
    "media_key" TEXT,
    "type" TEXT,
    "url" TEXT,
    "media_url" TEXT,
    "display_url" TEXT,
    "expanded_url" TEXT,
    "width" INTEGER,
    "height" INTEGER,
    "duration_ms" INTEGER,
    "alt_text" TEXT,

    CONSTRAINT "tweet_media_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweet_mentions" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "user_id" BIGINT,
    "screen_name" TEXT NOT NULL,
    "name" TEXT,
    "start_index" INTEGER,
    "end_index" INTEGER,

    CONSTRAINT "tweet_mentions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweet_hashtags" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "tag" TEXT NOT NULL,
    "start_index" INTEGER,
    "end_index" INTEGER,

    CONSTRAINT "tweet_hashtags_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweet_urls" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "url" TEXT NOT NULL,
    "expanded_url" TEXT,
    "display_url" TEXT,
    "start_index" INTEGER,
    "end_index" INTEGER,

    CONSTRAINT "tweet_urls_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "tweet_symbols" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "symbol" TEXT NOT NULL,
    "start_index" INTEGER,
    "end_index" INTEGER,

    CONSTRAINT "tweet_symbols_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "stream_events" (
    "id" SERIAL NOT NULL,
    "event_type" TEXT NOT NULL,
    "rule_id" TEXT,
    "rule_tag" TEXT,
    "tweet_count" INTEGER,
    "timestamp" BIGINT,
    "received_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "raw_payload" JSONB,

    CONSTRAINT "stream_events_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "scoring" (
    "id" SERIAL NOT NULL,
    "tweet_id" BIGINT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "start_time" TIMESTAMPTZ,
    "validator_hotkey" TEXT,
    "score" DOUBLE PRECISION,
    "epoch" INTEGER,
    "miner_hotkey" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "scoring_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "penalties" (
    "id" SERIAL NOT NULL,
    "hotkey" TEXT NOT NULL,
    "reason" TEXT NOT NULL,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "penalties_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "rewards" (
    "id" SERIAL NOT NULL,
    "start_block" INTEGER NOT NULL,
    "stop_block" INTEGER NOT NULL,
    "hotkey" TEXT NOT NULL,
    "points" DOUBLE PRECISION NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "rewards_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "blacklisted_hotkeys" (
    "hotkey" TEXT NOT NULL,
    "reason" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "blacklisted_hotkeys_pkey" PRIMARY KEY ("hotkey")
);

-- CreateTable
CREATE TABLE "tao_usd_price" (
    "id" SERIAL NOT NULL,
    "tao_price" DOUBLE PRECISION NOT NULL,
    "date" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "market_cap" DOUBLE PRECISION,
    "volume_24h" DOUBLE PRECISION,
    "price_change_24h" DOUBLE PRECISION,
    "market_cap_change_24h" DOUBLE PRECISION,
    "price_change_7d" DOUBLE PRECISION,
    "price_change_30d" DOUBLE PRECISION,
    "source" TEXT,

    CONSTRAINT "tao_usd_price_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "telegram_groups" (
    "id" TEXT NOT NULL,
    "telegram_id" BIGINT NOT NULL,
    "title" TEXT NOT NULL,
    "is_monitored" BOOLEAN NOT NULL DEFAULT false,
    "is_muted" BOOLEAN NOT NULL DEFAULT false,
    "muted_until" TIMESTAMP(3),
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "telegram_groups_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "telegram_messages" (
    "id" TEXT NOT NULL,
    "telegram_id" BIGINT NOT NULL,
    "group_id" TEXT NOT NULL,
    "sender_id" BIGINT NOT NULL,
    "sender_username" TEXT,
    "sender_name" TEXT NOT NULL,
    "content" TEXT NOT NULL,
    "reply_to_id" BIGINT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "telegram_messages_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "telegram_message_analysis" (
    "id" SERIAL NOT NULL,
    "message_id" TEXT NOT NULL,
    "sentiment" TEXT,
    "subnet_id" INTEGER,
    "subnet_name" TEXT,
    "asset_id" INTEGER,
    "asset_symbol" TEXT,
    "content_type" TEXT,
    "technical_quality" TEXT,
    "market_analysis" TEXT,
    "impact_potential" TEXT,
    "relevance_confidence" TEXT,
    "analysis_data" JSONB,
    "miner_hotkey" TEXT,
    "analyzed_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "telegram_message_analysis_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "telegram_scoring" (
    "id" SERIAL NOT NULL,
    "message_id" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "start_time" TIMESTAMP(3),
    "validator_hotkey" TEXT,
    "score" DOUBLE PRECISION,
    "epoch" INTEGER,
    "miner_hotkey" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "telegram_scoring_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "news_articles" (
    "id" SERIAL NOT NULL,
    "url" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "summary" TEXT,
    "content" TEXT,
    "raw_html" TEXT,
    "published" TIMESTAMPTZ,
    "source" TEXT NOT NULL,
    "topic" TEXT,
    "extra" JSONB,
    "rule_tag" TEXT,
    "source_type" TEXT NOT NULL DEFAULT 'rss',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "news_articles_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "analysis_variants" (
    "id" SERIAL NOT NULL,
    "article_id" INTEGER NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "analysis_data" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "analysis_variants_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "news_article_analysis" (
    "id" SERIAL NOT NULL,
    "article_id" INTEGER NOT NULL,
    "sentiment" TEXT,
    "sector_id" INTEGER,
    "sector_symbol" TEXT,
    "content_type" TEXT,
    "technical_quality" TEXT,
    "market_analysis" TEXT,
    "impact_potential" TEXT,
    "relevance_confidence" TEXT,
    "mentioned_assets" JSONB,
    "analysis_data" JSONB,
    "miner_hotkey" TEXT,
    "analyzed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "impact_level" TEXT,
    "factual_confidence" TEXT,
    "event_type" TEXT,
    "event_date" DATE,
    "content_hash" TEXT,
    "primary_geo" TEXT,
    "overall_sentiment_score" DOUBLE PRECISION,
    "narrative_embedding" DOUBLE PRECISION[] DEFAULT ARRAY[]::DOUBLE PRECISION[],

    CONSTRAINT "news_article_analysis_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "news_article_scoring" (
    "id" SERIAL NOT NULL,
    "article_id" INTEGER NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "start_time" TIMESTAMPTZ,
    "validator_hotkey" TEXT,
    "score" DOUBLE PRECISION,
    "epoch" INTEGER,
    "miner_hotkey" TEXT,
    "title" TEXT,
    "published" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "news_article_scoring_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "events" (
    "id" SERIAL NOT NULL,
    "fingerprint" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "canonical_title" TEXT NOT NULL,
    "event_date" DATE,
    "sector_id" INTEGER,
    "sector_symbol" TEXT,
    "article_count" INTEGER NOT NULL DEFAULT 1,
    "sentiment" TEXT,
    "impact_potential" TEXT,
    "entities" JSONB,
    "summary" TEXT,
    "title_embedding" DOUBLE PRECISION[] DEFAULT ARRAY[]::DOUBLE PRECISION[],
    "first_seen_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_article_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "events_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "event_articles" (
    "id" SERIAL NOT NULL,
    "event_id" INTEGER NOT NULL,
    "article_id" INTEGER NOT NULL,
    "role" TEXT NOT NULL DEFAULT 'reporting',
    "added_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "event_articles_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "narratives" (
    "id" SERIAL NOT NULL,
    "slug" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "keywords" JSONB NOT NULL,
    "sector_ids" JSONB,
    "sentiment_bias" TEXT,
    "phase" TEXT NOT NULL DEFAULT 'emerging',
    "article_count" INTEGER NOT NULL DEFAULT 0,
    "event_count" INTEGER NOT NULL DEFAULT 0,
    "sentiment_score" DOUBLE PRECISION,
    "momentum" DOUBLE PRECISION,
    "source" TEXT NOT NULL DEFAULT 'seed',
    "embedding" DOUBLE PRECISION[] DEFAULT ARRAY[]::DOUBLE PRECISION[],
    "embedding_model" TEXT,
    "start_date" DATE,
    "last_article_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "narratives_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "narrative_articles" (
    "id" SERIAL NOT NULL,
    "narrative_id" INTEGER NOT NULL,
    "article_id" INTEGER NOT NULL,
    "confidence" DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    "matched_keyword" TEXT,
    "added_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "narrative_articles_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "event_narratives" (
    "id" SERIAL NOT NULL,
    "event_id" INTEGER NOT NULL,
    "narrative_id" INTEGER NOT NULL,
    "added_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "event_narratives_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "narrative_intensity" (
    "id" SERIAL NOT NULL,
    "narrative_id" INTEGER NOT NULL,
    "date" DATE NOT NULL,
    "article_count" INTEGER NOT NULL DEFAULT 0,
    "avg_sentiment" DOUBLE PRECISION,
    "source_count" INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT "narrative_intensity_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "narrative_candidates" (
    "id" SERIAL NOT NULL,
    "keyword" TEXT NOT NULL,
    "article_count" INTEGER NOT NULL DEFAULT 1,
    "source_count" INTEGER NOT NULL DEFAULT 1,
    "first_seen_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_seen_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "promoted" BOOLEAN NOT NULL DEFAULT false,
    "embedding" DOUBLE PRECISION[] DEFAULT ARRAY[]::DOUBLE PRECISION[],

    CONSTRAINT "narrative_candidates_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "score_verdict" (
    "id" SERIAL NOT NULL,
    "resource_type" TEXT NOT NULL,
    "resource_id" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "miner_signature" TEXT NOT NULL,
    "miner_analysis_hash" TEXT NOT NULL,
    "validator_verdict" TEXT NOT NULL,
    "categorical_key" TEXT NOT NULL,
    "points_awarded" DOUBLE PRECISION NOT NULL,
    "is_audit" BOOLEAN NOT NULL DEFAULT false,
    "audit_group_id" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "score_verdict_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "attestation" (
    "id" SERIAL NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "per_miner_points" JSONB NOT NULL,
    "total_points" DOUBLE PRECISION NOT NULL,
    "merkle_root" TEXT NOT NULL,
    "signature" TEXT NOT NULL,
    "issued_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "attestation_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "broadcast_report" (
    "id" SERIAL NOT NULL,
    "reporter_hotkey" TEXT NOT NULL,
    "accused_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "reason" TEXT NOT NULL,
    "evidence" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "broadcast_report_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "penalty_detail" (
    "id" SERIAL NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "resource_type" TEXT NOT NULL,
    "resource_id" TEXT NOT NULL,
    "cause" TEXT NOT NULL,
    "failed_fields" JSONB,
    "miner_values" JSONB,
    "validator_values" JSONB,
    "post_preview" TEXT,
    "score" DOUBLE PRECISION,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "penalty_detail_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "reputation_snapshot" (
    "id" BIGSERIAL NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "reputation" DOUBLE PRECISION NOT NULL,
    "samples" INTEGER NOT NULL DEFAULT 0,
    "gate" DOUBLE PRECISION,
    "projected_share" DOUBLE PRECISION,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "reputation_snapshot_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "miner_event" (
    "id" BIGSERIAL NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "streak" TEXT,
    "shadow" BOOLEAN,
    "epoch" INTEGER,
    "reason" TEXT,
    "detail" JSONB,
    "occurred_at" TIMESTAMPTZ NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "miner_event_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "mechanism_profile" (
    "version" INTEGER NOT NULL,
    "publish_block" INTEGER NOT NULL,
    "activation_block" INTEGER NOT NULL,
    "schema_version" TEXT NOT NULL,
    "body" JSONB NOT NULL,
    "signature" TEXT NOT NULL,
    "published_by" TEXT,
    "published_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "mechanism_profile_pkey" PRIMARY KEY ("version")
);

-- CreateTable
CREATE TABLE "controller_proposal" (
    "id" BIGSERIAL NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "roi_ema" DOUBLE PRECISION NOT NULL,
    "direction" INTEGER NOT NULL,
    "magnitude" DOUBLE PRECISION NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "controller_proposal_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "idx_accounts_screen_name" ON "accounts"("screen_name");

-- CreateIndex
CREATE INDEX "idx_accounts_user_name" ON "accounts"("user_name");

-- CreateIndex
CREATE UNIQUE INDEX "monitored_accounts_screen_name_key" ON "monitored_accounts"("screen_name");

-- CreateIndex
CREATE UNIQUE INDEX "monitored_accounts_account_id_key" ON "monitored_accounts"("account_id");

-- CreateIndex
CREATE INDEX "idx_monitored_screen_name" ON "monitored_accounts"("screen_name");

-- CreateIndex
CREATE INDEX "idx_monitored_active" ON "monitored_accounts"("is_active");

-- CreateIndex
CREATE UNIQUE INDEX "source_accounts_screen_name_key" ON "source_accounts"("screen_name");

-- CreateIndex
CREATE INDEX "idx_relationships_source" ON "relationships"("source_account");

-- CreateIndex
CREATE INDEX "idx_relationships_target" ON "relationships"("target_account_id");

-- CreateIndex
CREATE INDEX "idx_relationships_type" ON "relationships"("relationship_type");

-- CreateIndex
CREATE UNIQUE INDEX "relationships_source_account_target_account_id_relationship_key" ON "relationships"("source_account", "target_account_id", "relationship_type");

-- CreateIndex
CREATE INDEX "idx_tweets_author" ON "tweets"("author_id");

-- CreateIndex
CREATE INDEX "idx_tweets_created" ON "tweets"("created_at");

-- CreateIndex
CREATE INDEX "idx_tweets_conversation" ON "tweets"("conversation_id");

-- CreateIndex
CREATE INDEX "idx_tweets_in_reply_to" ON "tweets"("in_reply_to_id");

-- CreateIndex
CREATE INDEX "idx_tweets_quoted" ON "tweets"("quoted_tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweets_retweeted" ON "tweets"("retweeted_tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweets_rule" ON "tweets"("rule_id");

-- CreateIndex
CREATE UNIQUE INDEX "tweet_analysis_tweet_id_key" ON "tweet_analysis"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_analysis_sentiment" ON "tweet_analysis"("sentiment");

-- CreateIndex
CREATE INDEX "idx_tweet_analysis_subnet" ON "tweet_analysis"("subnet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_analysis_asset" ON "tweet_analysis"("asset_id");

-- CreateIndex
CREATE INDEX "idx_tweet_analysis_analyzed_at" ON "tweet_analysis"("analyzed_at");

-- CreateIndex
CREATE INDEX "idx_tweet_media_tweet" ON "tweet_media"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_media_type" ON "tweet_media"("type");

-- CreateIndex
CREATE UNIQUE INDEX "tweet_media_tweet_id_media_id_key" ON "tweet_media"("tweet_id", "media_id");

-- CreateIndex
CREATE INDEX "idx_tweet_mentions_tweet" ON "tweet_mentions"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_mentions_user" ON "tweet_mentions"("user_id");

-- CreateIndex
CREATE INDEX "idx_tweet_mentions_screen_name" ON "tweet_mentions"("screen_name");

-- CreateIndex
CREATE UNIQUE INDEX "tweet_mentions_tweet_id_user_id_key" ON "tweet_mentions"("tweet_id", "user_id");

-- CreateIndex
CREATE INDEX "idx_tweet_hashtags_tweet" ON "tweet_hashtags"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_hashtags_tag" ON "tweet_hashtags"("tag");

-- CreateIndex
CREATE UNIQUE INDEX "tweet_hashtags_tweet_id_tag_key" ON "tweet_hashtags"("tweet_id", "tag");

-- CreateIndex
CREATE INDEX "idx_tweet_urls_tweet" ON "tweet_urls"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_symbols_tweet" ON "tweet_symbols"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_tweet_symbols_symbol" ON "tweet_symbols"("symbol");

-- CreateIndex
CREATE UNIQUE INDEX "tweet_symbols_tweet_id_symbol_key" ON "tweet_symbols"("tweet_id", "symbol");

-- CreateIndex
CREATE INDEX "idx_stream_events_type" ON "stream_events"("event_type");

-- CreateIndex
CREATE INDEX "idx_stream_events_received" ON "stream_events"("received_at");

-- CreateIndex
CREATE INDEX "idx_scoring_tweet" ON "scoring"("tweet_id");

-- CreateIndex
CREATE INDEX "idx_scoring_status" ON "scoring"("status");

-- CreateIndex
CREATE INDEX "idx_scoring_validator" ON "scoring"("validator_hotkey");

-- CreateIndex
CREATE INDEX "idx_scoring_status_start_time" ON "scoring"("status", "start_time");

-- CreateIndex
CREATE INDEX "idx_penalties_hotkey" ON "penalties"("hotkey");

-- CreateIndex
CREATE INDEX "idx_penalties_timestamp" ON "penalties"("timestamp");

-- CreateIndex
CREATE INDEX "idx_rewards_hotkey" ON "rewards"("hotkey");

-- CreateIndex
CREATE INDEX "idx_rewards_blocks" ON "rewards"("start_block", "stop_block");

-- CreateIndex
CREATE INDEX "idx_tao_usd_price_date" ON "tao_usd_price"("date");

-- CreateIndex
CREATE UNIQUE INDEX "telegram_groups_telegram_id_key" ON "telegram_groups"("telegram_id");

-- CreateIndex
CREATE INDEX "idx_telegram_groups_telegram_id" ON "telegram_groups"("telegram_id");

-- CreateIndex
CREATE INDEX "idx_telegram_groups_monitored" ON "telegram_groups"("is_monitored");

-- CreateIndex
CREATE UNIQUE INDEX "telegram_messages_telegram_id_key" ON "telegram_messages"("telegram_id");

-- CreateIndex
CREATE INDEX "idx_telegram_messages_group" ON "telegram_messages"("group_id");

-- CreateIndex
CREATE INDEX "idx_telegram_messages_created" ON "telegram_messages"("created_at");

-- CreateIndex
CREATE INDEX "idx_telegram_messages_reply_to" ON "telegram_messages"("reply_to_id");

-- CreateIndex
CREATE INDEX "idx_telegram_messages_sender" ON "telegram_messages"("sender_id");

-- CreateIndex
CREATE UNIQUE INDEX "telegram_message_analysis_message_id_key" ON "telegram_message_analysis"("message_id");

-- CreateIndex
CREATE INDEX "idx_telegram_analysis_sentiment" ON "telegram_message_analysis"("sentiment");

-- CreateIndex
CREATE INDEX "idx_telegram_analysis_subnet" ON "telegram_message_analysis"("subnet_id");

-- CreateIndex
CREATE INDEX "idx_telegram_analysis_asset" ON "telegram_message_analysis"("asset_id");

-- CreateIndex
CREATE INDEX "idx_telegram_analysis_analyzed_at" ON "telegram_message_analysis"("analyzed_at");

-- CreateIndex
CREATE INDEX "idx_telegram_scoring_message" ON "telegram_scoring"("message_id");

-- CreateIndex
CREATE INDEX "idx_telegram_scoring_status" ON "telegram_scoring"("status");

-- CreateIndex
CREATE INDEX "idx_telegram_scoring_validator" ON "telegram_scoring"("validator_hotkey");

-- CreateIndex
CREATE INDEX "idx_telegram_scoring_status_start_time" ON "telegram_scoring"("status", "start_time");

-- CreateIndex
CREATE UNIQUE INDEX "news_articles_url_key" ON "news_articles"("url");

-- CreateIndex
CREATE INDEX "idx_news_articles_source" ON "news_articles"("source");

-- CreateIndex
CREATE INDEX "idx_news_articles_published" ON "news_articles"("published");

-- CreateIndex
CREATE INDEX "idx_news_articles_created" ON "news_articles"("created_at");

-- CreateIndex
CREATE INDEX "idx_news_articles_source_type" ON "news_articles"("source_type");

-- CreateIndex
CREATE INDEX "idx_variant_article" ON "analysis_variants"("article_id");

-- CreateIndex
CREATE UNIQUE INDEX "analysis_variants_article_id_miner_hotkey_key" ON "analysis_variants"("article_id", "miner_hotkey");

-- CreateIndex
CREATE UNIQUE INDEX "news_article_analysis_article_id_key" ON "news_article_analysis"("article_id");

-- CreateIndex
CREATE INDEX "idx_news_analysis_sentiment" ON "news_article_analysis"("sentiment");

-- CreateIndex
CREATE INDEX "idx_news_analysis_sector" ON "news_article_analysis"("sector_id");

-- CreateIndex
CREATE INDEX "idx_news_analysis_analyzed_at" ON "news_article_analysis"("analyzed_at");

-- CreateIndex
CREATE INDEX "idx_news_analysis_event_type" ON "news_article_analysis"("event_type");

-- CreateIndex
CREATE INDEX "idx_news_analysis_event_date" ON "news_article_analysis"("event_date");

-- CreateIndex
CREATE INDEX "idx_news_analysis_content_hash" ON "news_article_analysis"("content_hash");

-- CreateIndex
CREATE INDEX "idx_news_analysis_impact" ON "news_article_analysis"("impact_level");

-- CreateIndex
CREATE INDEX "idx_news_scoring_article" ON "news_article_scoring"("article_id");

-- CreateIndex
CREATE INDEX "idx_news_scoring_status" ON "news_article_scoring"("status");

-- CreateIndex
CREATE INDEX "idx_news_scoring_validator" ON "news_article_scoring"("validator_hotkey");

-- CreateIndex
CREATE INDEX "idx_news_scoring_status_start_time" ON "news_article_scoring"("status", "start_time");

-- CreateIndex
CREATE INDEX "idx_news_scoring_title_status" ON "news_article_scoring"("title", "status");

-- CreateIndex
CREATE UNIQUE INDEX "events_fingerprint_key" ON "events"("fingerprint");

-- CreateIndex
CREATE INDEX "idx_events_type" ON "events"("event_type");

-- CreateIndex
CREATE INDEX "idx_events_date" ON "events"("event_date");

-- CreateIndex
CREATE INDEX "idx_events_sector" ON "events"("sector_id");

-- CreateIndex
CREATE INDEX "idx_events_last_article" ON "events"("last_article_at");

-- CreateIndex
CREATE INDEX "idx_event_articles_event" ON "event_articles"("event_id");

-- CreateIndex
CREATE INDEX "idx_event_articles_article" ON "event_articles"("article_id");

-- CreateIndex
CREATE UNIQUE INDEX "event_articles_event_id_article_id_key" ON "event_articles"("event_id", "article_id");

-- CreateIndex
CREATE UNIQUE INDEX "narratives_slug_key" ON "narratives"("slug");

-- CreateIndex
CREATE INDEX "idx_narratives_phase" ON "narratives"("phase");

-- CreateIndex
CREATE INDEX "idx_narratives_last_article" ON "narratives"("last_article_at");

-- CreateIndex
CREATE INDEX "idx_narrative_articles_narrative" ON "narrative_articles"("narrative_id");

-- CreateIndex
CREATE INDEX "idx_narrative_articles_article" ON "narrative_articles"("article_id");

-- CreateIndex
CREATE UNIQUE INDEX "narrative_articles_narrative_id_article_id_key" ON "narrative_articles"("narrative_id", "article_id");

-- CreateIndex
CREATE INDEX "idx_event_narratives_event" ON "event_narratives"("event_id");

-- CreateIndex
CREATE INDEX "idx_event_narratives_narrative" ON "event_narratives"("narrative_id");

-- CreateIndex
CREATE UNIQUE INDEX "event_narratives_event_id_narrative_id_key" ON "event_narratives"("event_id", "narrative_id");

-- CreateIndex
CREATE INDEX "idx_narrative_intensity_narrative" ON "narrative_intensity"("narrative_id");

-- CreateIndex
CREATE INDEX "idx_narrative_intensity_date" ON "narrative_intensity"("date");

-- CreateIndex
CREATE UNIQUE INDEX "narrative_intensity_narrative_id_date_key" ON "narrative_intensity"("narrative_id", "date");

-- CreateIndex
CREATE UNIQUE INDEX "narrative_candidates_keyword_key" ON "narrative_candidates"("keyword");

-- CreateIndex
CREATE INDEX "idx_narrative_candidates_keyword" ON "narrative_candidates"("keyword");

-- CreateIndex
CREATE INDEX "idx_narrative_candidates_count" ON "narrative_candidates"("article_count");

-- CreateIndex
CREATE INDEX "idx_score_verdict_epoch_validator" ON "score_verdict"("epoch", "validator_hotkey");

-- CreateIndex
CREATE INDEX "idx_score_verdict_audit_group" ON "score_verdict"("audit_group_id");

-- CreateIndex
CREATE UNIQUE INDEX "score_verdict_resource_type_resource_id_validator_hotkey_ep_key" ON "score_verdict"("resource_type", "resource_id", "validator_hotkey", "epoch");

-- CreateIndex
CREATE UNIQUE INDEX "attestation_validator_hotkey_epoch_key" ON "attestation"("validator_hotkey", "epoch");

-- CreateIndex
CREATE INDEX "idx_broadcast_report_accused" ON "broadcast_report"("accused_hotkey", "epoch");

-- CreateIndex
CREATE UNIQUE INDEX "broadcast_report_reporter_hotkey_accused_hotkey_epoch_reaso_key" ON "broadcast_report"("reporter_hotkey", "accused_hotkey", "epoch", "reason");

-- CreateIndex
CREATE INDEX "idx_penalty_detail_miner_epoch" ON "penalty_detail"("miner_hotkey", "epoch");

-- CreateIndex
CREATE INDEX "idx_reputation_snapshot_miner_epoch" ON "reputation_snapshot"("miner_hotkey", "epoch");

-- CreateIndex
CREATE INDEX "idx_reputation_snapshot_validator_epoch" ON "reputation_snapshot"("validator_hotkey", "epoch");

-- CreateIndex
CREATE INDEX "idx_miner_event_miner_time" ON "miner_event"("miner_hotkey", "occurred_at");

-- CreateIndex
CREATE INDEX "idx_mechanism_profile_activation" ON "mechanism_profile"("activation_block");

-- CreateIndex
CREATE INDEX "idx_controller_proposal_epoch" ON "controller_proposal"("epoch");

-- AddForeignKey
ALTER TABLE "monitored_accounts" ADD CONSTRAINT "monitored_accounts_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "accounts"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "relationships" ADD CONSTRAINT "relationships_target_account_id_fkey" FOREIGN KEY ("target_account_id") REFERENCES "accounts"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweets" ADD CONSTRAINT "tweets_author_id_fkey" FOREIGN KEY ("author_id") REFERENCES "accounts"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweets" ADD CONSTRAINT "tweets_quoted_tweet_id_fkey" FOREIGN KEY ("quoted_tweet_id") REFERENCES "tweets"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweets" ADD CONSTRAINT "tweets_retweeted_tweet_id_fkey" FOREIGN KEY ("retweeted_tweet_id") REFERENCES "tweets"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweet_analysis" ADD CONSTRAINT "tweet_analysis_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweet_media" ADD CONSTRAINT "tweet_media_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweet_mentions" ADD CONSTRAINT "tweet_mentions_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweet_hashtags" ADD CONSTRAINT "tweet_hashtags_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweet_urls" ADD CONSTRAINT "tweet_urls_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "tweet_symbols" ADD CONSTRAINT "tweet_symbols_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "scoring" ADD CONSTRAINT "scoring_tweet_id_fkey" FOREIGN KEY ("tweet_id") REFERENCES "tweets"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "telegram_messages" ADD CONSTRAINT "telegram_messages_group_id_fkey" FOREIGN KEY ("group_id") REFERENCES "telegram_groups"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "telegram_message_analysis" ADD CONSTRAINT "telegram_message_analysis_message_id_fkey" FOREIGN KEY ("message_id") REFERENCES "telegram_messages"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "telegram_scoring" ADD CONSTRAINT "telegram_scoring_message_id_fkey" FOREIGN KEY ("message_id") REFERENCES "telegram_messages"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "news_article_analysis" ADD CONSTRAINT "news_article_analysis_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "news_article_scoring" ADD CONSTRAINT "news_article_scoring_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_articles" ADD CONSTRAINT "event_articles_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_articles" ADD CONSTRAINT "event_articles_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "narrative_articles" ADD CONSTRAINT "narrative_articles_narrative_id_fkey" FOREIGN KEY ("narrative_id") REFERENCES "narratives"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "narrative_articles" ADD CONSTRAINT "narrative_articles_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_narratives" ADD CONSTRAINT "event_narratives_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_narratives" ADD CONSTRAINT "event_narratives_narrative_id_fkey" FOREIGN KEY ("narrative_id") REFERENCES "narratives"("id") ON DELETE CASCADE ON UPDATE CASCADE;

