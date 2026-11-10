-- AlterTable
ALTER TABLE "claim" ADD COLUMN     "body" TEXT,
ADD COLUMN     "doc_hash" TEXT,
ADD COLUMN     "held" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "judged_at" TIMESTAMPTZ(3),
ADD COLUMN     "novelty" TEXT,
ADD COLUMN     "pending_of_event_id" INTEGER,
ADD COLUMN     "receipt_bucket" BIGINT,
ADD COLUMN     "route" TEXT,
ADD COLUMN     "settle_at" TIMESTAMPTZ(3),
ADD COLUMN     "signed_at" INTEGER,
ADD COLUMN     "strike" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "verified_at" TIMESTAMPTZ(3),
ALTER COLUMN "status" SET DEFAULT 'received';

-- AlterTable
ALTER TABLE "ledger_entry" ADD COLUMN     "category" TEXT,
ADD COLUMN     "factors" JSONB,
ADD COLUMN     "route" TEXT;

-- CreateTable
CREATE TABLE "job" (
    "id" BIGSERIAL NOT NULL,
    "kind" TEXT NOT NULL,
    "claim_id" BIGINT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "run_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "locked_at" TIMESTAMPTZ(3),
    "error" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "job_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "idx_job_status_kind_run" ON "job"("status", "kind", "run_at");

-- CreateIndex
CREATE INDEX "idx_job_claim" ON "job"("claim_id");

-- CreateIndex
CREATE INDEX "idx_claim_status_bucket" ON "claim"("status", "receipt_bucket");

-- CreateIndex
CREATE INDEX "idx_claim_judged" ON "claim"("judged_at");

-- CreateIndex
CREATE UNIQUE INDEX "claim_hotkey_body_hash_key" ON "claim"("hotkey", "body_hash");

-- AddForeignKey
ALTER TABLE "job" ADD CONSTRAINT "job_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE CASCADE ON UPDATE CASCADE;

