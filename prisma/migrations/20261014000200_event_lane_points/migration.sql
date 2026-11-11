-- AlterTable
ALTER TABLE "attestation" ADD COLUMN     "capacity" DOUBLE PRECISION,
ADD COLUMN     "leaf_count" INTEGER,
ADD COLUMN     "pools" JSONB;

-- CreateTable
CREATE TABLE "category_weight" (
    "id" SERIAL NOT NULL,
    "week_start" DATE NOT NULL,
    "category" TEXT NOT NULL,
    "weight" DOUBLE PRECISION NOT NULL,
    "target_share" DOUBLE PRECISION NOT NULL,
    "actual_share" DOUBLE PRECISION,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "category_weight_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "reverify_result" (
    "id" SERIAL NOT NULL,
    "claim_id" BIGINT NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "verdict" TEXT NOT NULL,
    "observed" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "reverify_result_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "reverify_pull" (
    "id" SERIAL NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "reverify_pull_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "category_weight_week_start_category_key" ON "category_weight"("week_start", "category");

-- CreateIndex
CREATE INDEX "idx_reverify_epoch" ON "reverify_result"("epoch");

-- CreateIndex
CREATE UNIQUE INDEX "reverify_result_claim_id_validator_hotkey_key" ON "reverify_result"("claim_id", "validator_hotkey");

-- CreateIndex
CREATE INDEX "idx_reverify_pull_validator_epoch" ON "reverify_pull"("validator_hotkey", "epoch");

-- AddForeignKey
ALTER TABLE "reverify_result" ADD CONSTRAINT "reverify_result_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE CASCADE ON UPDATE CASCADE;

