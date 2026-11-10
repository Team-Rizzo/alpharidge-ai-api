-- Launch categories: financial and market become finance. Ledger rows are append-only
-- and keep the category they were written with.

-- Keyed tables keep one row per key, preferring the former financial row.
DELETE FROM "source_trust" m USING "source_trust" f
 WHERE m."category" = 'market' AND f."category" = 'financial' AND m."source_id" = f."source_id";
DELETE FROM "miner_accuracy" m USING "miner_accuracy" f
 WHERE m."category" = 'market' AND f."category" = 'financial' AND m."hotkey" = f."hotkey";
DELETE FROM "category_weight" m USING "category_weight" f
 WHERE m."category" = 'market' AND f."category" = 'financial' AND m."week_start" = f."week_start";

UPDATE "source_trust" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');
UPDATE "miner_accuracy" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');
UPDATE "category_weight" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');
UPDATE "events" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');
UPDATE "source_event" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');
UPDATE "answer_key_item" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');
UPDATE "accuracy_check" SET "category" = 'finance' WHERE "category" IN ('financial', 'market');

UPDATE "claim" SET "category" = 'finance',
       "payload" = jsonb_set("payload", '{category}', '"finance"')
 WHERE "category" IN ('financial', 'market');
UPDATE "feed_record" SET "category" = 'finance',
       "payload" = CASE WHEN "payload" ? 'category'
                        THEN jsonb_set("payload", '{category}', '"finance"') ELSE "payload" END
 WHERE "category" IN ('financial', 'market');

UPDATE "source"
   SET "category_trust" = ("category_trust" - 'financial' - 'market')
       || jsonb_build_object('finance',
                             COALESCE("category_trust" -> 'financial', "category_trust" -> 'market'))
 WHERE "category_trust" ?| ARRAY['financial', 'market'];
