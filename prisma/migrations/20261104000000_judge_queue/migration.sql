-- Claims and found pages waiting for the listed validators' page verdicts.
CREATE TABLE "judge_item" (
    "id" BIGSERIAL NOT NULL,
    "key" TEXT NOT NULL,
    "claim_id" BIGINT NOT NULL,
    "kind" TEXT NOT NULL,
    "hotkey" TEXT,
    "task" JSONB NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "created_at" TIMESTAMPTZ(3) NOT NULL,
    CONSTRAINT "judge_item_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "judge_item_key_key" ON "judge_item"("key");
CREATE INDEX "idx_judge_item_status" ON "judge_item"("status", "id");
CREATE INDEX "idx_judge_item_hotkey" ON "judge_item"("hotkey", "created_at");

-- Each validator's signed verdict on a judge item, once per item.
CREATE TABLE "judge_post" (
    "id" BIGSERIAL NOT NULL,
    "item_id" BIGINT NOT NULL,
    "validator" TEXT NOT NULL,
    "body" TEXT NOT NULL,
    "signed_at" BIGINT NOT NULL,
    "nonce" TEXT NOT NULL,
    "signature" TEXT NOT NULL,
    "posted_at" TIMESTAMPTZ(3) NOT NULL,
    CONSTRAINT "judge_post_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "judge_post_item_id_validator_key" ON "judge_post"("item_id", "validator");
