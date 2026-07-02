-- Backfill the denormalized news_article_scoring.published and build the index that
-- lets path A order the pending pool without a sort. Run in psql AUTOCOMMIT (not
-- prisma migrate — per-batch COMMIT + CREATE INDEX CONCURRENTLY need to run outside a
-- transaction). Prereq: the ADD COLUMN published migration applied and the write-safe
-- API deploy live (new rows already set published). This fills pre-existing rows.

-- 1) Backfill published in committed batches via a moving PK cursor (single ordered
--    pass; no re-scan of the shrinking NULL set). Defensively also fills any NULL
--    title, so this is safe to run standalone.
CREATE OR REPLACE PROCEDURE backfill_scoring_published(batch_size int DEFAULT 10000)
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
    SET published = COALESCE(s.published, a.published),
        title     = COALESCE(s.title, a.title)
    FROM news_articles a
    WHERE s.id > last_id AND s.id <= batch_max
      AND a.id = s.article_id
      AND (s.published IS NULL OR s.title IS NULL);
    GET DIAGNOSTICS updated = ROW_COUNT;

    total   := total + updated;
    last_id := batch_max;
    COMMIT;
    RAISE NOTICE 'backfill: through id % (+% this batch, % total)', last_id, updated, total;
  END LOOP;
END $$;

CALL backfill_scoring_published(10000);

-- 2) Gate: every row that has an article must now have published. Expect 0.
--    (Rows can legitimately be NULL only if the source article's published is NULL —
--    that's fine; ORDER BY ... NULLS LAST handles it. This counts rows where we FAILED
--    to copy a non-null article.published.)
SELECT count(*) AS unbackfilled
FROM news_article_scoring s JOIN news_articles a ON a.id = s.article_id
WHERE s.published IS NULL AND a.published IS NOT NULL;

-- 3) The index path A orders by — a PARTIAL index on the pending set in published
--    order. Two details are load-bearing (verified via EXPLAIN ANALYZE on 500k):
--      * WHERE status='pending' — so the index IS the pending set (status alone can't
--        filter, since every path-A candidate is 'pending').
--      * published DESC NULLS LAST — MUST match path A's ORDER BY exactly. A plain
--        `DESC` index is NULLS FIRST; the mismatch makes the planner ignore the index
--        and seq-scan+sort the whole pool. This one detail is the difference between
--        an index-scan halt at 150 and a full-pool sort.
--    Result: Nested Loop Anti Join driven by this index, reads ~150 rows, dedup intact.
--    CONCURRENTLY (outside a txn); can take a while on a large pending pool.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scoring_pending_published
  ON news_article_scoring (published DESC NULLS LAST, id DESC)
  WHERE status = 'pending';

-- 4) Confirm valid before flipping the path-A read switch.
SELECT c.relname, i.indisvalid
FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.relname = 'idx_scoring_pending_published';

-- Cleanup (optional): DROP PROCEDURE backfill_scoring_published(int);
