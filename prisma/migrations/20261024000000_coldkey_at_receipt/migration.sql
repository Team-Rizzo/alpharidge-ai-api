-- The miner's coldkey at receipt, on the claim and its ledger rows.
ALTER TABLE "claim" ADD COLUMN "coldkey" TEXT;
ALTER TABLE "ledger_entry" ADD COLUMN "coldkey" TEXT;
