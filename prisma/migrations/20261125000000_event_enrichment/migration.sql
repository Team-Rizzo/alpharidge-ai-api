-- Summary, sentiment and place of published events, one row per event.
CREATE TABLE "event_enrichment" (
    "event_id" INTEGER NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "backfill" BOOLEAN NOT NULL DEFAULT false,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "run_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "locked_at" TIMESTAMPTZ(3),
    "confidence" TEXT,
    "sentiment_score" DOUBLE PRECISION,
    "country" TEXT,
    "region" TEXT,
    "cost_usd" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "error" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "done_at" TIMESTAMPTZ(3),
    CONSTRAINT "event_enrichment_pkey" PRIMARY KEY ("event_id")
);
CREATE INDEX "idx_event_enrichment_due" ON "event_enrichment"("status", "backfill", "run_at");

-- Enrichment spend per UTC day.
CREATE TABLE "enrichment_spend" (
    "day" DATE NOT NULL,
    "usd" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "calls" INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT "enrichment_spend_pkey" PRIMARY KEY ("day")
);
