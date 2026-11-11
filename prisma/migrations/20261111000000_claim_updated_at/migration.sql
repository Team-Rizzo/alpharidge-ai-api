-- When a claim last changed, for each miner's change feed.
ALTER TABLE "claim" ADD COLUMN "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX "idx_claim_hotkey_updated" ON "claim"("hotkey", "updated_at", "id");

CREATE FUNCTION claim_touch() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER claim_touch BEFORE UPDATE ON "claim"
    FOR EACH ROW EXECUTE FUNCTION claim_touch();
