-- When a claim was paid or rejected. Coldkey quality is read in this order, so a claim paid
-- late does not land in history that was already served.
--
-- Only the column here: adding a nullable column without a default is a catalog change, but it
-- needs a brief ACCESS EXCLUSIVE lock on "claim". The backfill is scripts/backfill_decided_at.py
-- (batched, run while the API is live) and the index is built CONCURRENTLY two migrations on.
--
-- Prisma sends this file as one query, one transaction, on the session it uses for every
-- pending migration. Each attempt waits at most 2 s for the lock instead of queueing behind a
-- long query (and holding up every reader queued behind the ALTER), then retries after a
-- pause; after 10 attempts (about 50 s) the migration fails and nothing has changed (DEPLOY.md).
-- set_config(..., true) is transaction-local, so the timeout does not carry into the
-- CREATE INDEX CONCURRENTLY migration that runs next on the same session.
DO $$
BEGIN
    FOR attempt IN 1..10 LOOP
        BEGIN
            PERFORM set_config('lock_timeout', '2s', true);
            ALTER TABLE "claim" ADD COLUMN "decided_at" TIMESTAMPTZ(3);
            RETURN;
        EXCEPTION WHEN lock_not_available THEN
            RAISE NOTICE 'claim busy, ALTER attempt % of 10 timed out', attempt;
            IF attempt < 10 THEN
                PERFORM pg_sleep(3);
            END IF;
        END;
    END LOOP;
    RAISE EXCEPTION 'could not lock "claim" in 10 attempts: end the long transaction, then prisma migrate resolve --rolled-back 20261117000000_claim_decided_at and deploy again (DEPLOY.md)';
END
$$;
