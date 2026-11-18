-- An update that changes decided_at and nothing else (the decided_at backfill) keeps the
-- claim's updated_at, so it does not put every decided claim back in its miner's change feed
-- (GET /claims reads by updated_at). Any other change still stamps updated_at, as before.
-- Replacing the function takes no lock on "claim".
CREATE OR REPLACE FUNCTION claim_touch() RETURNS trigger AS $$
BEGIN
    IF NEW.decided_at IS DISTINCT FROM OLD.decided_at
       AND to_jsonb(NEW) - 'decided_at' - 'updated_at'
           = to_jsonb(OLD) - 'decided_at' - 'updated_at' THEN
        NEW.updated_at = OLD.updated_at;
        RETURN NEW;
    END IF;
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
