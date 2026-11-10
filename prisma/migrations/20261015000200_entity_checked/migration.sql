-- When a registry entity was last looked up.
ALTER TABLE "entity" ADD COLUMN "checked_at" TIMESTAMPTZ;
