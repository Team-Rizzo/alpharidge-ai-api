-- Ledger rows carry their claim's receipt time and bucket; re-verify reports are recorded.
ALTER TABLE "ledger_entry" ADD COLUMN "received_ms" BIGINT;
ALTER TABLE "ledger_entry" ADD COLUMN "receipt_bucket" BIGINT;
ALTER TABLE "reverify_pull" ADD COLUMN "reported_at" TIMESTAMPTZ(3);
