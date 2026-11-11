-- Signed claim receipts, one per stored claim; each names the previous receipt's hash.
CREATE TABLE "claim_receipt" (
    "seq" BIGINT NOT NULL,
    "version" INTEGER NOT NULL,
    "prev" TEXT NOT NULL,
    "claim_id" BIGINT NOT NULL,
    "hotkey" TEXT NOT NULL,
    "body_sha256" TEXT NOT NULL,
    "received_ms" BIGINT NOT NULL,
    "hash" TEXT NOT NULL,
    "signature" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "claim_receipt_pkey" PRIMARY KEY ("seq")
);

CREATE UNIQUE INDEX "claim_receipt_claim_id_key" ON "claim_receipt"("claim_id");
CREATE UNIQUE INDEX "claim_receipt_hash_key" ON "claim_receipt"("hash");

-- Receipts are append-only
CREATE FUNCTION claim_receipt_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'claim_receipt is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER claim_receipt_no_update_delete
    BEFORE UPDATE OR DELETE ON "claim_receipt"
    FOR EACH ROW EXECUTE FUNCTION claim_receipt_append_only();
