-- Reputation tracking telemetry: per-hotkey reputation snapshots pushed by validators.
-- DECOUPLED from the consensus/scoring pipeline by design — this table is never read by
-- get_attestation / get_verdicts / write_verdict / merkle_root and carries no signatures,
-- hashes, or points. Additive only: creates one new table + two indexes, alters nothing.

CREATE TABLE IF NOT EXISTS "reputation_snapshot" (
    "id" BIGSERIAL PRIMARY KEY,
    "validator_hotkey" TEXT NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "epoch" INTEGER NOT NULL,
    "reputation" DOUBLE PRECISION NOT NULL,
    "samples" INTEGER NOT NULL DEFAULT 0,
    "gate" DOUBLE PRECISION,
    "projected_share" DOUBLE PRECISION,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_reputation_snapshot_miner_epoch" ON "reputation_snapshot"("miner_hotkey", "epoch");
CREATE INDEX IF NOT EXISTS "idx_reputation_snapshot_validator_epoch" ON "reputation_snapshot"("validator_hotkey", "epoch");
