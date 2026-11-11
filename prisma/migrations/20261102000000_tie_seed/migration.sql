-- The tie block of each receipt bucket that held more than one claim.
CREATE TABLE "tie_seed" (
    "bucket" BIGINT NOT NULL,
    "block" BIGINT NOT NULL,
    "block_hash" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "tie_seed_pkey" PRIMARY KEY ("bucket")
);
