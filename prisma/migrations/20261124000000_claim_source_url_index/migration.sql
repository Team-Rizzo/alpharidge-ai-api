-- Claims by their page, so a second report of a page finds the event the first one opened.
-- A hash index: URLs can be longer than a btree entry allows. Built CONCURRENTLY, so claim
-- reads and writes go on; CREATE INDEX CONCURRENTLY cannot run in a transaction block, so this
-- file holds this one statement only. If it fails it leaves an INVALID index:
-- DROP INDEX CONCURRENTLY "idx_claim_source_url", mark the migration rolled back and deploy
-- again (DEPLOY.md).
CREATE INDEX CONCURRENTLY "idx_claim_source_url" ON "claim" USING HASH ("source_url");
