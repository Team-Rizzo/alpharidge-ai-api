-- Per-miner dispatch/cooldown event log for the miner dashboard.
-- DECOUPLED from the consensus/scoring pipeline by design — this table is never read by
-- get_attestation / get_verdicts / write_verdict / merkle_root and carries no signatures,
-- hashes, or points. It records discrete dispatch-state transitions (parked/unparked/
-- batch_size_changed/reward_zeroed/chronic_timeout) so a miner can see when and why their
-- dispatch state changed. Additive only: creates one new table + one index, alters nothing.

CREATE TABLE IF NOT EXISTS "miner_event" (
    "id" BIGSERIAL PRIMARY KEY,
    "validator_hotkey" TEXT NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "streak" TEXT,
    "shadow" BOOLEAN,
    "epoch" INTEGER,
    "reason" TEXT,
    "detail" JSONB,
    "occurred_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_miner_event_miner_time" ON "miner_event"("miner_hotkey", "occurred_at");
