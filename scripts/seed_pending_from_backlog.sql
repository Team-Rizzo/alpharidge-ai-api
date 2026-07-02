-- One-time seed of the servable-ready-queue from the existing article backlog, so
-- path A (the fast index walk) serves everything and path B goes truly vestigial.
-- This is the formal version of the metered manual bridge. Run in psql AUTOCOMMIT
-- (per-batch COMMIT). Prereq: published denorm live + writers routing new articles
-- into pending + the poison purge run. min_content MUST match the API's
-- MIN_ARTICLE_CONTENT_CHARS.
--
-- Queues one servable copy per title (DISTINCT ON title, newest published) for
-- articles that (a) have no scoring row yet and (b) whose title is not already in
-- the pipeline. Batched by news_articles.id cursor. Idempotent + resumable: re-runs
-- skip anything already queued (NOT EXISTS guards), so it's safe to run repeatedly
-- until it drains the backlog.

CREATE OR REPLACE PROCEDURE seed_pending_from_backlog(batch_size int DEFAULT 5000, min_content int DEFAULT 200)
LANGUAGE plpgsql AS $$
DECLARE
  last_id   bigint := 0;
  batch_max bigint;
  inserted  int;
  total     bigint := 0;
BEGIN
  LOOP
    SELECT max(id) INTO batch_max
    FROM (SELECT id FROM news_articles WHERE id > last_id ORDER BY id LIMIT batch_size) w;
    EXIT WHEN batch_max IS NULL;

    WITH cand AS (
      SELECT DISTINCT ON (a.title) a.id, a.title, a.published
      FROM news_articles a
      WHERE a.id > last_id AND a.id <= batch_max
        AND a.source_type IN ('rss','ccnews')
        AND a.content IS NOT NULL AND length(btrim(a.content)) >= min_content
        AND a.title IS NOT NULL AND btrim(a.title) <> ''
        AND NOT EXISTS (SELECT 1 FROM news_article_scoring s  WHERE s.article_id = a.id)   -- no scoring yet
        AND NOT EXISTS (SELECT 1 FROM news_article_scoring s2 WHERE s2.title = a.title)     -- title not in pipeline
      ORDER BY a.title, a.published DESC NULLS LAST
    ), ins AS (
      INSERT INTO news_article_scoring (article_id, title, published, status)
      SELECT id, title, published, 'pending' FROM cand
      RETURNING article_id
    )
    SELECT count(*) INTO inserted FROM ins;

    total   := total + inserted;
    last_id := batch_max;
    COMMIT;
    RAISE NOTICE 'seed: through article id % (+% queued, % total)', last_id, inserted, total;
  END LOOP;
END $$;

CALL seed_pending_from_backlog(5000, 200);

SELECT count(*) AS servable_pending FROM news_article_scoring WHERE status='pending';
