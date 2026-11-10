-- AlterTable
ALTER TABLE "source_fetch" ADD COLUMN     "bytes" INTEGER,
ADD COLUMN     "canonical_url" TEXT,
ADD COLUMN     "final_url" TEXT,
ADD COLUMN     "page_type" TEXT,
ADD COLUMN     "published_at" TIMESTAMPTZ(3),
ADD COLUMN     "reason" TEXT,
ADD COLUMN     "simhash" BIGINT;

-- CreateTable
CREATE TABLE "llm_spend" (
    "id" SERIAL NOT NULL,
    "day" DATE NOT NULL,
    "purpose" TEXT NOT NULL,
    "calls" INTEGER NOT NULL DEFAULT 0,
    "usd" DOUBLE PRECISION NOT NULL DEFAULT 0,

    CONSTRAINT "llm_spend_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "llm_spend_day_purpose_key" ON "llm_spend"("day", "purpose");
