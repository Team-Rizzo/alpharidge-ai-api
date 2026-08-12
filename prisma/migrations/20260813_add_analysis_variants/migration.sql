-- Analyses of the same article from overlap verifier miners. Append-only and
-- additive: creates one new table, alters nothing existing.

CREATE TABLE IF NOT EXISTS "analysis_variants" (
    "id" SERIAL PRIMARY KEY,
    "article_id" INTEGER NOT NULL,
    "miner_hotkey" TEXT NOT NULL,
    "analysis_data" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS "uq_variant_article_miner"
    ON "analysis_variants" ("article_id", "miner_hotkey");
CREATE INDEX IF NOT EXISTS "idx_variant_article"
    ON "analysis_variants" ("article_id");
