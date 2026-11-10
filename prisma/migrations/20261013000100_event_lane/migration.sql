-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "vector";

-- AlterTable
ALTER TABLE "events" ADD COLUMN     "category" TEXT,
ADD COLUMN     "embedding" vector(384),
ADD COLUMN     "first_claim_id" BIGINT,
ADD COLUMN     "identity_key" TEXT;

-- CreateTable
CREATE TABLE "claim" (
    "id" BIGSERIAL NOT NULL,
    "hotkey" TEXT NOT NULL,
    "signature" TEXT NOT NULL,
    "nonce" TEXT NOT NULL,
    "body_hash" TEXT NOT NULL,
    "received_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "kind" TEXT NOT NULL,
    "category" TEXT NOT NULL,
    "event_type" TEXT,
    "payload" JSONB NOT NULL,
    "source_url" TEXT NOT NULL,
    "source_id" INTEGER,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "reason" TEXT,
    "event_id" INTEGER,

    CONSTRAINT "claim_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "entity" (
    "id" SERIAL NOT NULL,
    "scheme" TEXT NOT NULL,
    "external_id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "aliases" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "entity_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "event_entity" (
    "event_id" INTEGER NOT NULL,
    "entity_id" INTEGER NOT NULL,
    "role" TEXT,

    CONSTRAINT "event_entity_pkey" PRIMARY KEY ("event_id","entity_id")
);

-- CreateTable
CREATE TABLE "event_fact" (
    "id" BIGSERIAL NOT NULL,
    "event_id" INTEGER NOT NULL,
    "claim_id" BIGINT,
    "field" TEXT NOT NULL,
    "value" JSONB NOT NULL,
    "verified" BOOLEAN NOT NULL DEFAULT false,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "event_fact_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "source" (
    "id" SERIAL NOT NULL,
    "domain" TEXT NOT NULL,
    "path_rule" TEXT NOT NULL DEFAULT '',
    "owner_id" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "base_trust" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "category_trust" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "source_fetch" (
    "id" BIGSERIAL NOT NULL,
    "claim_id" BIGINT NOT NULL,
    "http_status" INTEGER,
    "content_hash" TEXT,
    "verdict" TEXT NOT NULL,
    "spans" JSONB,
    "fetched_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_fetch_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ledger_entry" (
    "id" BIGSERIAL NOT NULL,
    "hotkey" TEXT NOT NULL,
    "claim_id" BIGINT,
    "event_id" INTEGER,
    "kind" TEXT NOT NULL,
    "points" DOUBLE PRECISION NOT NULL,
    "epoch" INTEGER NOT NULL,
    "reason" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ledger_entry_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "answer_key_item" (
    "id" BIGSERIAL NOT NULL,
    "adapter" TEXT NOT NULL,
    "external_id" TEXT NOT NULL,
    "payload" JSONB NOT NULL,
    "matched_claim_id" BIGINT,
    "observed_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "answer_key_item_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "webhook_subscription" (
    "id" SERIAL NOT NULL,
    "owner_id" TEXT NOT NULL,
    "url" TEXT NOT NULL,
    "secret" TEXT NOT NULL,
    "categories" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "event_types" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "active" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "webhook_subscription_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "idx_claim_body_hash" ON "claim"("body_hash");

-- CreateIndex
CREATE INDEX "idx_claim_status_received" ON "claim"("status", "received_at");

-- CreateIndex
CREATE INDEX "idx_claim_hotkey_received" ON "claim"("hotkey", "received_at");

-- CreateIndex
CREATE INDEX "idx_claim_event" ON "claim"("event_id");

-- CreateIndex
CREATE UNIQUE INDEX "claim_hotkey_nonce_key" ON "claim"("hotkey", "nonce");

-- CreateIndex
CREATE INDEX "idx_entity_name" ON "entity"("name");

-- CreateIndex
CREATE UNIQUE INDEX "entity_scheme_external_id_key" ON "entity"("scheme", "external_id");

-- CreateIndex
CREATE INDEX "idx_event_entity_entity" ON "event_entity"("entity_id");

-- CreateIndex
CREATE INDEX "idx_event_fact_event_field" ON "event_fact"("event_id", "field");

-- CreateIndex
CREATE INDEX "idx_event_fact_claim" ON "event_fact"("claim_id");

-- CreateIndex
CREATE INDEX "idx_source_status" ON "source"("status");

-- CreateIndex
CREATE UNIQUE INDEX "source_domain_path_rule_key" ON "source"("domain", "path_rule");

-- CreateIndex
CREATE INDEX "idx_source_fetch_claim" ON "source_fetch"("claim_id");

-- CreateIndex
CREATE INDEX "idx_ledger_epoch_hotkey" ON "ledger_entry"("epoch", "hotkey");

-- CreateIndex
CREATE INDEX "idx_ledger_hotkey_created" ON "ledger_entry"("hotkey", "created_at");

-- CreateIndex
CREATE INDEX "idx_ledger_claim" ON "ledger_entry"("claim_id");

-- CreateIndex
CREATE INDEX "idx_answer_key_claim" ON "answer_key_item"("matched_claim_id");

-- CreateIndex
CREATE UNIQUE INDEX "answer_key_item_adapter_external_id_key" ON "answer_key_item"("adapter", "external_id");

-- CreateIndex
CREATE INDEX "idx_webhook_subscription_owner" ON "webhook_subscription"("owner_id");

-- CreateIndex
CREATE UNIQUE INDEX "events_identity_key_key" ON "events"("identity_key");

-- CreateIndex
CREATE UNIQUE INDEX "events_first_claim_id_key" ON "events"("first_claim_id");

-- CreateIndex
CREATE INDEX "idx_events_category_date" ON "events"("category", "event_date");

-- AddForeignKey
ALTER TABLE "events" ADD CONSTRAINT "events_first_claim_id_fkey" FOREIGN KEY ("first_claim_id") REFERENCES "claim"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "claim" ADD CONSTRAINT "claim_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "source"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "claim" ADD CONSTRAINT "claim_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_entity" ADD CONSTRAINT "event_entity_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_entity" ADD CONSTRAINT "event_entity_entity_id_fkey" FOREIGN KEY ("entity_id") REFERENCES "entity"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_fact" ADD CONSTRAINT "event_fact_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_fact" ADD CONSTRAINT "event_fact_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_fetch" ADD CONSTRAINT "source_fetch_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ledger_entry" ADD CONSTRAINT "ledger_entry_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ledger_entry" ADD CONSTRAINT "ledger_entry_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "answer_key_item" ADD CONSTRAINT "answer_key_item_matched_claim_id_fkey" FOREIGN KEY ("matched_claim_id") REFERENCES "claim"("id") ON DELETE SET NULL ON UPDATE CASCADE;


-- CreateIndex
CREATE INDEX "idx_events_embedding" ON "events" USING hnsw ("embedding" vector_cosine_ops);

-- Ledger rows are append-only
CREATE FUNCTION ledger_entry_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entry is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_entry_no_update_delete
    BEFORE UPDATE OR DELETE ON "ledger_entry"
    FOR EACH ROW EXECUTE FUNCTION ledger_entry_append_only();
