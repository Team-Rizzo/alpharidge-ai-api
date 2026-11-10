-- AlterTable
ALTER TABLE "entity" ADD COLUMN     "kind" TEXT,
ADD COLUMN     "xrefs" TEXT[] DEFAULT ARRAY[]::TEXT[];

-- AlterTable
ALTER TABLE "event_fact" ADD COLUMN     "doc_hash" TEXT,
ADD COLUMN     "paid" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "slot" TEXT,
ADD COLUMN     "source_url" TEXT,
ADD COLUMN     "status" TEXT NOT NULL DEFAULT 'accepted';

-- AlterTable
ALTER TABLE "events" ADD COLUMN     "confidence" TEXT NOT NULL DEFAULT 'reported',
ADD COLUMN     "official_match" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "place_admin1" TEXT,
ADD COLUMN     "place_iso2" TEXT,
ADD COLUMN     "publish_at" TIMESTAMPTZ(3),
ADD COLUMN     "stage" TEXT,
ADD COLUMN     "storyline_id" INTEGER;

-- CreateTable
CREATE TABLE "event_stage" (
    "id" BIGSERIAL NOT NULL,
    "event_id" INTEGER NOT NULL,
    "stage" TEXT NOT NULL,
    "claim_id" BIGINT,
    "at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "event_stage_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "geo_place" (
    "geonames_id" INTEGER NOT NULL,
    "iso2" TEXT NOT NULL,
    "admin1" TEXT,
    "name" TEXT NOT NULL,
    "parent_id" INTEGER,

    CONSTRAINT "geo_place_pkey" PRIMARY KEY ("geonames_id")
);

-- CreateIndex
CREATE INDEX "idx_event_stage_claim" ON "event_stage"("claim_id");

-- CreateIndex
CREATE UNIQUE INDEX "event_stage_event_id_stage_key" ON "event_stage"("event_id", "stage");

-- CreateIndex
CREATE INDEX "idx_geo_place_admin1" ON "geo_place"("iso2", "admin1");

-- CreateIndex
CREATE INDEX "idx_event_fact_event_slot" ON "event_fact"("event_id", "slot");

-- AddForeignKey
ALTER TABLE "event_stage" ADD CONSTRAINT "event_stage_event_id_fkey" FOREIGN KEY ("event_id") REFERENCES "events"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "event_stage" ADD CONSTRAINT "event_stage_claim_id_fkey" FOREIGN KEY ("claim_id") REFERENCES "claim"("id") ON DELETE SET NULL ON UPDATE CASCADE;

