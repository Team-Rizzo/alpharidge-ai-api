-- Verifiable validator points: per-item verdicts, signed attestations,
-- broadcast discrepancy reports, and epoch/miner attribution on lease rows.

CREATE TABLE "score_verdict" (
    "id" SERIAL PRIMARY KEY,
    "resource_type" TEXT NOT NULL,
    "resource_id" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "miner_signature" TEXT NOT NULL,
    "miner_analysis_hash" TEXT NOT NULL,
    "validator_verdict" TEXT NOT NULL,
    "categorical_key" TEXT NOT NULL,
    "points_awarded" DOUBLE PRECISION NOT NULL,
    "is_audit" BOOLEAN NOT NULL DEFAULT false,
    "audit_group_id" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uq_score_verdict_item" UNIQUE ("resource_type", "resource_id", "validator_hotkey", "epoch")
);
CREATE INDEX "idx_score_verdict_epoch_validator" ON "score_verdict"("epoch", "validator_hotkey");
CREATE INDEX "idx_score_verdict_audit_group" ON "score_verdict"("audit_group_id");

CREATE TABLE "attestation" (
    "id" SERIAL PRIMARY KEY,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "per_miner_points" JSONB NOT NULL,
    "total_points" DOUBLE PRECISION NOT NULL,
    "merkle_root" TEXT NOT NULL,
    "signature" TEXT NOT NULL,
    "issued_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uq_attestation_validator_epoch" UNIQUE ("validator_hotkey", "epoch")
);

CREATE TABLE "broadcast_report" (
    "id" SERIAL PRIMARY KEY,
    "reporter_hotkey" TEXT NOT NULL,
    "accused_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "reason" TEXT NOT NULL,
    "evidence" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uq_broadcast_report" UNIQUE ("reporter_hotkey", "accused_hotkey", "epoch", "reason")
);
CREATE INDEX "idx_broadcast_report_accused" ON "broadcast_report"("accused_hotkey", "epoch");

ALTER TABLE "scoring" ADD COLUMN "epoch" INTEGER;
ALTER TABLE "scoring" ADD COLUMN "miner_hotkey" TEXT;
ALTER TABLE "telegram_scoring" ADD COLUMN "epoch" INTEGER;
ALTER TABLE "telegram_scoring" ADD COLUMN "miner_hotkey" TEXT;
ALTER TABLE "news_article_scoring" ADD COLUMN "epoch" INTEGER;
ALTER TABLE "news_article_scoring" ADD COLUMN "miner_hotkey" TEXT;
