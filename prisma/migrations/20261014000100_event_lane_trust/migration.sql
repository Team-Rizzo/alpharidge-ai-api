-- AlterTable
ALTER TABLE "entity" ADD COLUMN     "website" TEXT;

-- AlterTable
ALTER TABLE "source" ADD COLUMN     "reset_at" TIMESTAMPTZ;

-- CreateTable
CREATE TABLE "owner" (
    "id" TEXT NOT NULL,
    "evidence" JSONB NOT NULL DEFAULT '{}',
    "first_seen" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "owner_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "source_trust" (
    "id" SERIAL NOT NULL,
    "source_id" INTEGER NOT NULL,
    "category" TEXT NOT NULL,
    "base" DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    "earned" DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    "penalties" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "trust" DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    "judged_firsts" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "confirmed_firsts" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "confirm_owners" INTEGER NOT NULL DEFAULT 0,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_trust_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "source_event" (
    "id" BIGSERIAL NOT NULL,
    "source_id" INTEGER NOT NULL,
    "event_id" INTEGER,
    "claim_id" BIGINT,
    "owner_id" TEXT,
    "by_source_id" INTEGER,
    "role" TEXT NOT NULL,
    "category" TEXT NOT NULL,
    "published_at" TIMESTAMPTZ(3),
    "at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_event_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "page_rule" (
    "id" SERIAL NOT NULL,
    "pattern" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "reason" TEXT NOT NULL,
    "created_by" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "page_rule_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "trust_log" (
    "id" BIGSERIAL NOT NULL,
    "source_id" INTEGER,
    "rule_id" INTEGER,
    "field" TEXT NOT NULL,
    "old" TEXT,
    "new" TEXT,
    "reason" TEXT NOT NULL,
    "actor" TEXT NOT NULL,
    "at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "trust_log_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "source_trust_source_id_category_key" ON "source_trust"("source_id", "category");

-- CreateIndex
CREATE INDEX "idx_source_event_source" ON "source_event"("source_id");

-- CreateIndex
CREATE INDEX "idx_source_event_event" ON "source_event"("event_id");

-- CreateIndex
CREATE UNIQUE INDEX "page_rule_pattern_key" ON "page_rule"("pattern");

-- CreateIndex
CREATE INDEX "idx_trust_log_source" ON "trust_log"("source_id");

-- AddForeignKey
ALTER TABLE "source" ADD CONSTRAINT "source_owner_id_fkey" FOREIGN KEY ("owner_id") REFERENCES "owner"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_trust" ADD CONSTRAINT "source_trust_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "source"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_event" ADD CONSTRAINT "source_event_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "source"("id") ON DELETE CASCADE ON UPDATE CASCADE;

