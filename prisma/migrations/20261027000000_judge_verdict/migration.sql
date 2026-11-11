-- Page-judge verdicts, kept once per prompt version, model, claim and page content.
CREATE TABLE "judge_verdict" (
    "id" BIGSERIAL NOT NULL,
    "key" TEXT NOT NULL,
    "claim_id" BIGINT,
    "record" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "judge_verdict_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "judge_verdict_key_key" ON "judge_verdict"("key");
