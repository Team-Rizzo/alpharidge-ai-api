-- Add a nullable numeric score to penalty_detail so the dashboard can show how close a
-- rejected item was to passing (e.g. composite 0.62 vs the 0.65 bar, summary cosine 0.30
-- vs the 0.40 floor). Display-only, decoupled from consensus. Additive only: adds one
-- nullable column, alters/backfills nothing existing.

ALTER TABLE "penalty_detail" ADD COLUMN IF NOT EXISTS "score" DOUBLE PRECISION;
