-- Registry-resolved entities are cached with their status and frozen revision.
ALTER TABLE "entity" ADD COLUMN "status" TEXT NOT NULL DEFAULT 'linked';
ALTER TABLE "entity" ADD COLUMN "origin" TEXT NOT NULL DEFAULT 'seed';
ALTER TABLE "entity" ADD COLUMN "revision" TEXT;
