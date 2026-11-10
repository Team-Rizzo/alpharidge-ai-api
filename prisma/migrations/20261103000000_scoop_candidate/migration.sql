-- Pages validators found for open scoops.
CREATE TABLE "scoop_candidate" (
    "id" BIGSERIAL NOT NULL,
    "scoop_id" BIGINT NOT NULL,
    "url" TEXT NOT NULL,
    "validator" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'queued',
    "reason" TEXT,
    "found_at" TIMESTAMPTZ(3) NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "scoop_candidate_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "scoop_candidate_scoop_id_url_key" ON "scoop_candidate"("scoop_id", "url");
