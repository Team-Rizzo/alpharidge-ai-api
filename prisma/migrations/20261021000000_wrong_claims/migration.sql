-- Trust band frozen on the claim, weighted strikes, and wrong-claim marks.
ALTER TABLE "claim" ADD COLUMN "strike_weight" INTEGER NOT NULL DEFAULT 1;
ALTER TABLE "claim" ADD COLUMN "trust" DOUBLE PRECISION;
ALTER TABLE "claim" ADD COLUMN "trust_band" TEXT;
ALTER TABLE "claim" ADD COLUMN "wrong" TEXT;
ALTER TABLE "events" ADD COLUMN "marked" TEXT;
