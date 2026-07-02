# Runbook — /articles/unscored connection-pool exhaustion fix

**Repos:** `alpharidge-ai-api` (branch `fix/unscored-dedup-pool-exhaustion`, off/into `main`) **and**
`news-scraper` (branch `fix/scoring-title` — the scraper is a second live writer of the table).
⚠️ **news-scraper deploys from `article-intelligence-v2`, NOT main** — base and merge `fix/scoring-title`
into `article-intelligence-v2` (it currently equals main, but merging to main would not reach prod).
**Status:** built, syntax-clean, **not deployed** — parked for a dev to validate on prod.
**Owner note:** parallel to the depth canary; don't pull the depth prod dev off the canary for this.

## Problem (measured by prod dev, confirmed in code)

`GET /articles/unscored` 500s on ~30% of polls (Prisma pool exhaustion) → ~1/3 of work-fetch polls
deliver **zero** articles to miners. Supply is fine (~955k eligible). Root cause: the title dedup in
`get_unscored_articles` (main.py) checks
`NOT EXISTS (SELECT 1 FROM news_article_scoring s2 JOIN news_articles a2 ON a2.id=s2.article_id WHERE a2.title=a.title)`
— it **joins scoring→articles just to read the title**, so the planner materializes every in-pipeline
title (a hash anti-join over a full ~2M-row seq scan, EXPLAIN ~762k) **every poll**, pinning a
connection until the pool drains. That also 500s the shared-pool `POST /articles/completed`.

## Fix — denormalize the title onto `news_article_scoring`

Carry a denormalized `title` on the scoring row and dedup with an **index probe** instead of a join.
Titles are immutable post-ingest, so the copy never drifts. Behavior is identical — same titles
blocked — the plan just becomes an index scan of `published DESC` that stops at N.

**Two live writers of `news_article_scoring` (both must set title — this was the correctness gap):**
1. `alpharidge-ai-api` `get_unscored_articles` path B (main.py) — handled here.
2. `news-scraper/run.py` — the **primary** source of `pending` rows (path A leases exactly these);
   previously wrote `title=NULL`. Fixed in the `news-scraper` branch.
Plus **path-A self-heal**: the lease UPDATE sets `title = COALESCE(s.title, a.title)`, so any NULL
straggler is populated the moment it enters the `in_progress`/`completed` set the dedup probes.
(The dead `talisman-*` inserts are stopped procs — ignored.)

**Commits (API repo), staged for a read-safe rollout:**
- **`7949458` (phase 1)** — add nullable `title`; populate it at the API insert + path-A self-heal;
  reads still use the old join → behavior unchanged, deploy-safe on its own.
- **`0d1635f` (phase 2)** — switch both dedup NOT EXISTS to probe `s2.title`. The perf fix. Deploy
  only after every write path is live AND the backfill has populated every existing row.

## Deploy sequence (ordered — do not reorder)

1. **Deploy the `news-scraper` fix** (merge `fix/scoring-title` into **`article-intelligence-v2`** — the
   branch prod runs — then redeploy). Writes `title` on its pending insert; the primary writer stops
   injecting NULL titles.
2. **Apply the phase-1 migration** (`prisma/migrations/20260702_add_title_to_news_article_scoring/` —
   `ADD COLUMN title TEXT`, additive/instant/online).
3. **Deploy phase-1 API (`7949458`).** Now every new scoring row (both writers) has its title, and
   path-A leases self-heal. Verify `SELECT count(*) FILTER (WHERE title IS NOT NULL) FROM news_article_scoring;` climbs.
4. **Backfill + build the index:** run `scripts/backfill_scoring_title.sql` in **psql (autocommit)** —
   NOT via `prisma migrate` (batched COMMIT + `CREATE INDEX CONCURRENTLY` must run outside a txn). It
   backfills in batches, checks `remaining_null_title = 0`, builds `idx_news_scoring_title_status`
   CONCURRENTLY, and checks `indisvalid = true`.
5. **Gate before phase 2:** confirm `SELECT count(*) FROM news_article_scoring WHERE title IS NULL;` = 0
   AND the index is valid. If not, STOP — phase 2 is unsafe.
6. **Deploy phase-2 API (`0d1635f`).** Reads now probe the index. Done.

## Validation
- **Before/after EXPLAIN** on the path-B pick query: hash anti-join + 2M seq scan → index scan on
  `news_articles(published)` + probes on `idx_news_scoring_title_status`; cost ~762k → small.
- **Prod signals:** `/articles/unscored` + `/articles/completed` 500s → ~0; pool-timeout log lines
  stop; miners stop reporting "no work" in normal hours; supply-to-miners rises.
- **Correctness:** cloned-embedding / batch-zero rejects must NOT rise — confirms no same-title dup
  slipped through (i.e. every writer set title and the backfill held).
- **E2e:** `test_articles_unscored_serves_rss_and_ccnews` is a write-path test needing a real DB; run
  it against a live/test Postgres before prod. It was NOT run here (no DB in the build env).

## Stopgap (independent — can land first to stop the bleed today)
Prod config change (not in these branches): raise Prisma `connection_limit` and add a
`statement_timeout` so the slow anti-join fails fast and frees the connection instead of pinning the
pool. See the exact values in the handoff message.

## Rollback
- Phase 2 → redeploy phase-1 API (reads revert to the join; correct, just slow again).
- `title` column + index + the scraper/heal writes are additive and harmless; leave them.
