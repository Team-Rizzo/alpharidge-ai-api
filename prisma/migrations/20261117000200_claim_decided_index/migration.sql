-- Built CONCURRENTLY: no lock that blocks claim reads or writes. CREATE INDEX CONCURRENTLY
-- cannot run in a transaction block, and Prisma runs a file with several statements as one,
-- so this file must hold this one statement only. If it fails it leaves an INVALID index:
-- DROP INDEX CONCURRENTLY "idx_claim_decided", mark the migration rolled back and deploy again
-- (DEPLOY.md).
CREATE INDEX CONCURRENTLY "idx_claim_decided" ON "claim"("decided_at", "id");
