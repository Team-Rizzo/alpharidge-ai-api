-- AlterTable
ALTER TABLE "answer_key_item" ADD COLUMN     "category" TEXT,
ADD COLUMN     "event_date" DATE,
ADD COLUMN     "event_type" TEXT,
ADD COLUMN     "matched_at" TIMESTAMPTZ(3),
ADD COLUMN     "matched_event_id" INTEGER,
ADD COLUMN     "published_at" TIMESTAMPTZ(3),
ADD COLUMN     "status" TEXT NOT NULL DEFAULT 'open';

-- CreateTable
CREATE TABLE "accuracy_check" (
    "id" BIGSERIAL NOT NULL,
    "claim_id" BIGINT NOT NULL,
    "hotkey" TEXT NOT NULL,
    "category" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "ref" TEXT NOT NULL DEFAULT '',
    "correct" BOOLEAN NOT NULL,
    "at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "accuracy_check_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "miner_accuracy" (
    "id" SERIAL NOT NULL,
    "hotkey" TEXT NOT NULL,
    "category" TEXT NOT NULL,
    "n" INTEGER NOT NULL DEFAULT 0,
    "correct" INTEGER NOT NULL DEFAULT 0,
    "q" DOUBLE PRECISION NOT NULL,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "miner_accuracy_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "idx_accuracy_check_hotkey" ON "accuracy_check"("hotkey", "category", "at");

-- CreateIndex
CREATE UNIQUE INDEX "accuracy_check_claim_id_kind_ref_key" ON "accuracy_check"("claim_id", "kind", "ref");

-- CreateIndex
CREATE UNIQUE INDEX "miner_accuracy_hotkey_category_key" ON "miner_accuracy"("hotkey", "category");

-- CreateIndex
CREATE INDEX "idx_answer_key_status_observed" ON "answer_key_item"("status", "observed_at");

-- AddForeignKey
ALTER TABLE "accuracy_check" ADD CONSTRAINT "accuracy_check_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE CASCADE ON UPDATE CASCADE;

