-- Ledger rows stay append-only, with one exception: the seal may delete the carry rows of an
-- epoch that has no attestation row yet (neither sealed nor signed), to replace carry rows
-- that code from before the seal wrote and then stopped before signing. It asks for this
-- with SET LOCAL ledger.replace_carry = 'on', inside the seal's transaction.
-- Replacing the function takes no lock on "ledger_entry".
CREATE OR REPLACE FUNCTION ledger_entry_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.kind = 'carry'
       AND current_setting('ledger.replace_carry', true) = 'on'
       AND NOT EXISTS (SELECT 1 FROM attestation
                       WHERE validator_hotkey = 'event-ledger' AND epoch = OLD.epoch) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'ledger_entry is append-only';
END;
$$ LANGUAGE plpgsql;
