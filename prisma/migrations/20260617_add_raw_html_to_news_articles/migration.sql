-- Preserve the raw HTML of each article (before plain-text stripping) so the
-- ingest pipeline can run trafilatura main-content extraction on real DOM
-- structure and so articles can be re-processed later without re-fetching.
-- Additive + nullable: safe to apply online; existing rows keep raw_html NULL.
ALTER TABLE "news_articles" ADD COLUMN IF NOT EXISTS "raw_html" TEXT;
