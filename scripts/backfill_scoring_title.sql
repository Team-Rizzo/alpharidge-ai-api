-- Out-of-band ops for the /articles/unscored pool-exhaustion fix.
-- Run these in psql with AUTOCOMMIT ON (the default for interactive psql) — NOT
-- inside a BEGIN/COMMIT block and NOT via `prisma migrate` (which wraps in a
-- transaction; CREATE INDEX CONCURRENTLY and per-batch COMMIT both require running
-- outside one). See 2026-07-02-unscored-dedup-fix-runbook.md for the full sequence.
--
-- Prereq: phase-1 migration applied (news_article_scoring.title column exists) and
-- the phase-1 app deployed (new scoring rows already write title). This backfills
-- the pre-existing rows.

-- 1) Backfill title on existing scoring rows, in committed batches so the ~1M-row
--    table isn't locked in one long transaction / WAL spike.
CREATE OR REPLACE PROCEDURE backfill_scoring_title(batch_size int DEFAULT 10000)
LANGUAGE plpgsql AS $$
DECLARE
  updated int;
  total   bigint := 0;
BEGIN
  LOOP
    UPDATE news_article_scoring s
    SET title = a.title
    FROM news_articles a
    WHERE s.article_id = a.id
      AND s.title IS NULL
      AND s.id IN (
        SELECT id FROM news_article_scoring
        WHERE title IS NULL
        ORDER BY id
        LIMIT batch_size
      );
    GET DIAGNOSTICS updated = ROW_COUNT;
    total := total + updated;
    COMMIT;  -- allowed inside a procedure invoked via CALL (not in an outer txn)
    RAISE NOTICE 'backfilled % (running total %)', updated, total;
    EXIT WHEN updated = 0;
  END LOOP;
END $$;

CALL backfill_scoring_title(10000);

-- 2) Verify no in-pipeline row was left NULL before switching the reads (a NULL
--    title would fail to block a same-title dup -> cloned-embedding batch-zeroing).
--    Expect 0.
SELECT COUNT(*) AS remaining_null_title
FROM news_article_scoring
WHERE title IS NULL;

-- 3) Build the dedup index without locking writes. CONCURRENTLY must run outside a
--    transaction; it can take a while on ~1M rows — that is expected.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_news_scoring_title_status
  ON news_article_scoring (title, status);

-- 4) Confirm the index is valid (indisvalid = true). A failed CONCURRENTLY build
--    leaves an invalid index that must be dropped and rebuilt.
SELECT c.relname, i.indisvalid
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.relname = 'idx_news_scoring_title_status';

-- Cleanup (optional): DROP PROCEDURE backfill_scoring_title(int);
