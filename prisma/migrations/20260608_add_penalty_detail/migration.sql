-- Miner dashboard scoring transparency: display-only penalty attribution.
-- DECOUPLED from the consensus pipeline by design — this table is never read by
-- get_attestation / get_verdicts / write_verdict / merkle_root and carries no
-- signatures, hashes, or points. Additive only: creates one new table + one index,
-- alters nothing existing.

CREATE TABLE IF NOT EXISTS "penalty_detail" (
    "id" SERIAL PRIMARY KEY,
    "miner_hotkey" TEXT NOT NULL,
    "validator_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "resource_type" TEXT NOT NULL,
    "resource_id" TEXT NOT NULL,
    "cause" TEXT NOT NULL,
    "failed_fields" JSONB,
    "miner_values" JSONB,
    "validator_values" JSONB,
    "post_preview" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_penalty_detail_miner_epoch" ON "penalty_detail"("miner_hotkey", "epoch");
