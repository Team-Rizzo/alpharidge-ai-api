-- Add source_type column to distinguish RSS vs CC-NEWS articles
ALTER TABLE "news_articles" ADD COLUMN "source_type" TEXT NOT NULL DEFAULT 'rss';

-- Index for filtering by source type in scoring queries
CREATE INDEX "idx_news_articles_source_type" ON "news_articles"("source_type");
