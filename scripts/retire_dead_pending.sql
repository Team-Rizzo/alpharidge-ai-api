-- One-time purge of the poisoned pending pool (prod-measured: 22,058/22,092 pending
-- were dup-title rows the dedup excludes forever, so path A served 0 and path B
-- carried all traffic). SOFT-RETIRE (status='retired'), not DELETE — reversible and
-- auditable: `UPDATE ... SET status='pending' WHERE status='retired'` reverts it.
--
-- Retires EXACTLY the provably-unservable pending: (a) title already in_progress/
-- completed elsewhere (dedup-excluded forever), or (b) fails the servable content/
-- title filters. Leaves pending-only dup groups alone (path A + the response-level
-- seen_titles guard still drain one of those). Run in psql AUTOCOMMIT; batched by PK
-- cursor so the hot table isn't locked in one long txn. Requires phase-1's title
-- backfill complete (uses the denormalized s.title).

CREATE OR REPLACE PROCEDURE retire_dead_pending(batch_size int DEFAULT 10000)
LANGUAGE plpgsql AS $$
DECLARE
  last_id   bigint := 0;
  batch_max bigint;
  updated   int;
  total     bigint := 0;
BEGIN
  LOOP
    SELECT max(id) INTO batch_max
    FROM (SELECT id FROM news_article_scoring WHERE id > last_id ORDER BY id LIMIT batch_size) w;
    EXIT WHEN batch_max IS NULL;

    UPDATE news_article_scoring s
    SET status = 'retired'
    FROM news_articles a
    WHERE s.id > last_id AND s.id <= batch_max
      AND s.status = 'pending'
      AND a.id = s.article_id
      AND (
        EXISTS (SELECT 1 FROM news_article_scoring s2
                WHERE s2.title = s.title
                  AND s2.article_id <> s.article_id
                  AND s2.status IN ('in_progress', 'completed'))
        OR a.content IS NULL OR length(btrim(a.content)) < 200
        OR a.title IS NULL OR btrim(a.title) = ''
      );
    GET DIAGNOSTICS updated = ROW_COUNT;
    total := total + updated; last_id := batch_max;
    COMMIT;
    RAISE NOTICE 'retire: through id % (+% this batch, % total)', last_id, updated, total;
  END LOOP;
END $$;

-- Pre-count (sanity): how many pending are provably dead right now.
SELECT count(*) AS dead_pending_before
FROM news_article_scoring s JOIN news_articles a ON a.id = s.article_id
WHERE s.status='pending' AND (
  EXISTS (SELECT 1 FROM news_article_scoring s2 WHERE s2.title=s.title AND s2.article_id<>s.article_id AND s2.status IN ('in_progress','completed'))
  OR a.content IS NULL OR length(btrim(a.content)) < 200 OR a.title IS NULL OR btrim(a.title)='');

CALL retire_dead_pending(10000);

-- Post: servable pending should now be > 0 once ccnews routing + seed land.
SELECT count(*) AS pending_remaining FROM news_article_scoring WHERE status='pending';
SELECT count(*) AS retired FROM news_article_scoring WHERE status='retired';
