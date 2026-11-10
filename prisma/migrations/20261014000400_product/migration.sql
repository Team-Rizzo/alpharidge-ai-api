-- AlterTable
ALTER TABLE "events" ADD COLUMN     "hold" TEXT;

-- AlterTable
ALTER TABLE "source_fetch" ADD COLUMN     "check_result" JSONB;

-- AlterTable
ALTER TABLE "webhook_subscription" ADD COLUMN     "cursor" BIGINT NOT NULL DEFAULT 0;

-- CreateTable
CREATE TABLE "webhook_delivery" (
    "id" BIGSERIAL NOT NULL,
    "subscription_id" INTEGER NOT NULL,
    "seq" BIGINT NOT NULL,
    "body" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "next_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "error" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "webhook_delivery_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "feed_record" (
    "seq" BIGSERIAL NOT NULL,
    "at" TIMESTAMPTZ(3) NOT NULL,
    "kind" TEXT NOT NULL,
    "event_id" INTEGER NOT NULL,
    "category" TEXT NOT NULL,
    "event_type" TEXT NOT NULL,
    "payload" JSONB NOT NULL,

    CONSTRAINT "feed_record_pkey" PRIMARY KEY ("seq")
);

-- CreateTable
CREATE TABLE "api_key" (
    "id" SERIAL NOT NULL,
    "key_hash" TEXT NOT NULL,
    "owner_id" TEXT NOT NULL,
    "active" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "api_key_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "idx_webhook_delivery_status_next" ON "webhook_delivery"("status", "next_at");

-- CreateIndex
CREATE UNIQUE INDEX "webhook_delivery_subscription_id_seq_key" ON "webhook_delivery"("subscription_id", "seq");

-- CreateIndex
CREATE INDEX "idx_feed_record_event" ON "feed_record"("event_id");

-- CreateIndex
CREATE INDEX "idx_feed_record_at" ON "feed_record"("at");

-- CreateIndex
CREATE UNIQUE INDEX "api_key_key_hash_key" ON "api_key"("key_hash");

-- AddForeignKey
ALTER TABLE "webhook_delivery" ADD CONSTRAINT "webhook_delivery_subscription_id_fkey" FOREIGN KEY ("subscription_id") REFERENCES "webhook_subscription"("id") ON DELETE CASCADE ON UPDATE CASCADE;

