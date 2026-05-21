-- Add news article tables for the RSS news feed pipeline.
-- These support the article ingestion, classification, and scoring flow
-- parallel to the existing tweet and telegram pipelines.

CREATE TABLE "news_articles" (
    "id" SERIAL PRIMARY KEY,
    "url" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "summary" TEXT,
    "content" TEXT,
    "published" TIMESTAMPTZ,
    "source" TEXT NOT NULL,
    "topic" TEXT,
    "extra" JSONB,
    "rule_tag" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "news_articles_url_key" UNIQUE ("url")
);

CREATE INDEX "idx_news_articles_source" ON "news_articles"("source");
CREATE INDEX "idx_news_articles_published" ON "news_articles"("published");
CREATE INDEX "idx_news_articles_created" ON "news_articles"("created_at");

CREATE TABLE "news_article_analysis" (
    "id" SERIAL PRIMARY KEY,
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
    "analyzed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "news_article_analysis_article_id_key" UNIQUE ("article_id"),
    CONSTRAINT "news_article_analysis_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE CASCADE
);

CREATE INDEX "idx_news_article_analysis_sentiment" ON "news_article_analysis"("sentiment");
CREATE INDEX "idx_news_article_analysis_sector" ON "news_article_analysis"("sector_id");
CREATE INDEX "idx_news_article_analysis_analyzed_at" ON "news_article_analysis"("analyzed_at");

CREATE TABLE "news_article_scoring" (
    "id" SERIAL PRIMARY KEY,
    "article_id" INTEGER NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "start_time" TIMESTAMPTZ,
    "validator_hotkey" TEXT,
    "score" DOUBLE PRECISION,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "news_article_scoring_article_id_fkey" FOREIGN KEY ("article_id") REFERENCES "news_articles"("id") ON DELETE CASCADE
);

CREATE INDEX "idx_news_article_scoring_article" ON "news_article_scoring"("article_id");
CREATE INDEX "idx_news_article_scoring_status" ON "news_article_scoring"("status");
CREATE INDEX "idx_news_article_scoring_validator" ON "news_article_scoring"("validator_hotkey");
CREATE INDEX "idx_news_article_scoring_status_start_time" ON "news_article_scoring"("status", "start_time");
