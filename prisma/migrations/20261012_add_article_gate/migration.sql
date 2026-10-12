-- Downstream relevance gate: one verdict per article. Additive: creates one new
-- table, alters nothing existing.
--
-- eligible: passed the content rules and carries a relevant/borderline label.
-- relevant: judge verdict, NULL until checked. Downstream readers keep an article
-- when eligible AND relevant.

CREATE TABLE IF NOT EXISTS "article_gate" (
    "article_id"     INTEGER PRIMARY KEY REFERENCES "news_articles"("id") ON DELETE CASCADE,
    "eligible"       BOOLEAN NOT NULL,
    "reason"         TEXT,
    "miner_label"    TEXT,
    "relevant"       BOOLEAN,
    "judge_model"    TEXT,
    "judge_attempts" INTEGER NOT NULL DEFAULT 0,
    "judged_at"      TIMESTAMPTZ,
    "clustered_at"   TIMESTAMPTZ,
    "created_at"     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_article_gate_judged_at"
    ON "article_gate" ("judged_at");
