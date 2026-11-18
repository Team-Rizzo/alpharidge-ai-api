-- Read-only. Outcomes as the OLD API scores them (judged_at, all history) and the NEW one
-- (decided_at as the migration backfills it, last 30 days). Run on prod BEFORE the migration:
--   psql "$DATABASE_URL" -X -A -F, -t -f preview_budget.sql > outcomes.csv
-- JUNK = judge_reject,not_an_event,unsupported,page_type,scoop_stale (+ judge_unclear w/ type_none)
SELECT c.id, COALESCE(c.coldkey, c.hotkey) AS owner,
       (c.status IN ('provisional','settled','clawed_back'))::int AS passed,
       (extract(epoch FROM c.judged_at) * 1000)::bigint AS judged_ms,
       (extract(epoch FROM CASE WHEN c.status = 'rejected' THEN c.judged_at ELSE COALESCE(
           (SELECT min(l.created_at) FROM ledger_entry l WHERE l.claim_id = c.id AND l.kind = 'provisional'),
           c.judged_at) END) * 1000)::bigint AS decided_ms
FROM claim c
WHERE (c.status IN ('provisional','settled','clawed_back') OR (c.status = 'rejected' AND (
       c.reason IN ('judge_reject','not_an_event','unsupported','page_type','scoop_stale')
       OR (c.reason = 'judge_unclear' AND EXISTS (SELECT 1 FROM source_fetch sf WHERE sf.claim_id = c.id
           AND sf.check_result -> 'judge' ->> 'type_none' = 'true')))))
  AND c.judged_at IS NOT NULL;
