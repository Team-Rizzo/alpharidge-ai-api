# Deploying the claim decided_at change

This covers the release that adds `claim.decided_at` (coldkey quality is read in the order claims were decided). It ships four migrations and a backfill script:

| Migration | What it does | Lock on `claim` |
|---|---|---|
| `20261117000000_claim_decided_at` | Adds the nullable column. Each attempt waits at most 2 s for the lock, then retries after 3 s; it gives up after 10 attempts (about 50 s), changing nothing | ACCESS EXCLUSIVE, about 1 ms once it has it |
| `20261117000100_claim_touch_decided` | Changes the `claim_touch` trigger: an update that changes only `decided_at` keeps `updated_at`, so the backfill does not replay decided claims in miners' `GET /claims` feeds. Every other change stamps `updated_at` as before | none |
| `20261117000200_claim_decided_index` | `CREATE INDEX CONCURRENTLY idx_claim_decided` | SHARE UPDATE EXCLUSIVE (reads and writes go on) |
| `20261117000300_ledger_carry_replace` | Changes the `ledger_entry` append-only trigger: it still refuses every update and delete, except the seal's delete of the carry rows of an epoch with no `attestation` row (see "Carry left by the old writer" below) | none |

`scripts/backfill_decided_at.py` fills `decided_at` for claims decided before the column existed. It writes only claims whose `decided_at` is still NULL, 2000 ids per transaction, runs `VACUUM claim` after every 100,000 claims it fills, and can be stopped and run again at any time, with the old API or the new one live.

Measured on a scratch Postgres 16 with 1M claims (1.3 GB heap, 1.8 GB with indexes, on tmpfs), with a read and a write on `claim` every 100 ms beside it. Expect production disks to be 2 to 5 times slower.

| Step | Time | Reads and writes on `claim` |
|---|---|---|
| Column migration | 0.9 ms | not held up. With a transaction holding `claim` for 9 s, one attempt timed out: reads and writes waited 1.97 s once, and the second attempt succeeded |
| Trigger migration | 0.3 ms | not held up |
| Index migration | 0.8 to 0.9 s | not held up (slowest probe 19 ms) |
| Backfill, 900k decided claims | 75 to 81 s; each batch holds its rows' locks 130 ms (slowest 182 ms); each VACUUM 0.5 s | not held up (slowest probe 23 ms; a 200/s writer deciding and settling claims alongside: 0 failures, 0.6 ms average) |
| Backfill again with 500 claims left | 13 s (it reads every id range) | not held up |

The old single migration held ACCESS EXCLUSIVE for 36 s on the same data.

Claims with `decided_at` NULL are not counted by coldkey quality, so the new API works as soon as the column exists, with or without the index or the backfill; until the backfill is done it counts fewer claims. The old API works on the migrated schema (its store contract tests pass against it), which is what makes rollback safe.

### Also in this release: the ledger lock and seal

It changes how the API writes the ledger and signs epochs; its one migration is `20261117000300_ledger_carry_replace`.

- **Ledger lock.** Every ledger row and every claim decision takes a shared Postgres advisory lock (key 4545002). Signing an epoch takes it exclusively only to *seal* the epoch: one short transaction that writes the epoch's carry rows and an `attestation` row with `capacity` NULL and an empty root and signature. Its `issued_at` is the epoch's quality cut: one millisecond past both the database clock and the last seal. The window is then read (5000 rows per query) and hashed and signed with no lock or transaction held, and the signature fills that same row. Once sealed, an epoch gains no rows (later ones go to the next epoch), and a decision committed after the seal is stamped no earlier than its cut. A sealed epoch whose signing failed is signed again from the same rows on the next request. Measured on a scratch Postgres 16 with a 200k-row window, a pool of 9 and 40 concurrent deciders, payers, writers and readers: signing took 18 s. Before this change the lock was held for the whole signing: every decision and pay failed with "Timed out fetching a new connection", and writes waited up to 12.6 s. Now there were no errors, the slowest decision was 0.7 s and the slowest write 1.8 s. With a signing held for 70 s, a pay, a reject and a settle each committed at once; before, each timed out at 60 s.
- **Lock wait limit.** A transaction waits at most `LEDGER_LOCK_TIMEOUT_MS` (20 s, `app/db.py`) for the ledger lock, then fails and rolls back, well within the 60 s transaction limit. The same limit holds for the claim row locks taken later in that transaction. The claim stays where it was: the resolver and settle passes pay and settle it again, and confirm and re-check jobs are retried (60 s later, at most 3 runs; a re-check that clawed back already, or penalised the source already, does neither again). Known limit: a transaction that does not take the ledger lock (a claim's intake, which waits on the receipt lock) can wait up to the 60 s transaction limit, holding its pooled connection.
- **Carry left by the old writer.** The old code wrote an epoch's carry rows and then, in a separate step, signed the epoch. A writer stopped between the two (the restart in step 4 can do this) leaves some or all of the carry rows in an epoch with no attestation. The seal now deletes such rows and writes the epoch's carry in their place, in its one transaction under the exclusive lock (logged as `Epoch N: replaced K carry rows left unsigned`). Without this the new writer added a second copy: every 4.1.2 validator fails that epoch's carry check and the debt counts twice. The carry is read from the epochs before it, which are all sealed or signed by then, so none gains rows between reading it and the seal.
- **One move, one commit.** A claim's status move commits together with its ledger rows: pay, settle, clawback, penalty, and a scoop rejected with its debt. A failure leaves the claim where it was, and the next resolver or settle pass finishes it. A claim whose check fails goes from `novelty` straight to `rejected`, and the resolver rejects any `fetched` claim whose recorded check did not pass.
- **Pay retries.** A verified claim whose pay did not commit is paid by the resolver: an event claim that took its event, 10 minutes after it was verified, and a confirmed scoop when its window closes. A claim whose paid row is already written but which is still `verified` (left by a pay that was not atomic) is moved to `provisional` without a second row. See the pre-checks for what this pays on the first pass.

## 0. Update the code on the API host

Both repos change: the API, and the subnet checkout it imports (`alpharidge_ai`; the API reads `quality.QUALITY_FROM`, the carry rule and the claim check from it). Update both while the old processes run; they keep the code they loaded until they restart.

```bash
git -C <api checkout> pull                                    # this release
git -C <subnet checkout> fetch && git -C <subnet checkout> checkout <subnet release>
cd <api checkout>
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e "../alpharidge-ai[verify]"   # the subnet checkout, as in the README
.venv/bin/prisma generate                                      # the schema adds claim.decided_at
```

`prisma generate` must run before any process restarts: the new code reads `decided_at`, and a client generated from the old schema fails on it. The subnet release must be the one validators run (see "Once quality is on").

## 1. Pre-checks

Connect as the role in `DATABASE_URL` (the one `prisma migrate deploy` uses).

```sql
-- Size: the backfill rewrites every decided claim.
SELECT count(*) AS claims, pg_size_pretty(pg_relation_size('claim')) AS heap,
       pg_size_pretty(pg_total_relation_size('claim')) AS with_indexes FROM claim;

-- Both must be 0 (or longer than the index build). A lock_timeout cancels CREATE INDEX
-- CONCURRENTLY while it waits for older transactions, and leaves an INVALID index.
SHOW statement_timeout;
SHOW lock_timeout;

-- Long transactions: the column can't be added while one holds claim, and the index build
-- waits for every transaction older than itself. End or wait out anything listed.
SELECT pid, state, now() - xact_start AS open_for, left(query, 80) AS query
FROM pg_stat_activity
WHERE xact_start < now() - interval '1 minute' ORDER BY xact_start;

-- Epochs past the last signed one that already have carry rows: an old writer stopped while
-- signing. Expect none. The new writer replaces these rows when it seals the epoch.
SELECT DISTINCT epoch FROM ledger_entry WHERE kind = 'carry' AND epoch >
  (SELECT max(epoch) FROM attestation WHERE capacity IS NOT NULL AND validator_hotkey = 'event-ledger');
```

If the carry query lists an epoch, the running old writer will sign it with those rows as they are (its carry step skips an epoch that has any); restart the writer onto the new code (step 4) right away, so the new one signs it. Once the new writer runs, the query lists nothing, or only an epoch it is signing at that moment.

- **Disk:** keep at least the claim table's size with indexes free. With the VACUUM every 100k, the heap grew 10% and the table with indexes 14% in the test (without it, 90% and 75%). The backfill also writes about that much WAL; on a replica, throttle with `--pause`.
- **State:** `.venv/bin/prisma migrate status` should list exactly the four migrations above as not yet applied.
- Rehearse on a copy of production first.

What the first resolver pass after the writer restart will do (the old code never paid these):

```sql
-- (a) Half paid: the paid row is written, the claim is still verified. Each is moved to
-- provisional with no new row on the writer's first resolver pass, and settles from that row.
SELECT c.id, c.route, c.hotkey, l.epoch, l.points
FROM claim c JOIN ledger_entry l ON l.claim_id = c.id AND l.kind = 'provisional'
WHERE c.status = 'verified';

-- (b) Never paid: event claims that took their event, verified 10+ minutes ago with no paid
-- row. All are paid at once on the first pass, at their frozen trust and age.
SELECT count(*) AS claims, min(c.verified_at) AS oldest
FROM claim c JOIN events e ON e.first_claim_id = c.id
WHERE c.status = 'verified' AND c.route = 'event'
  AND c.verified_at < now() - interval '10 minutes'
  AND NOT EXISTS (SELECT 1 FROM ledger_entry l WHERE l.claim_id = c.id AND l.kind = 'provisional');

-- Confirmed scoops with no paid row: each is paid when its window closes (at once if it has).
SELECT count(*) FROM claim c
WHERE c.status = 'verified' AND c.route = 'scoop'
  AND EXISTS (SELECT 1 FROM source_fetch sf WHERE sf.claim_id = c.id AND sf.check_result -> 'judge' ? 'confirm')
  AND NOT EXISTS (SELECT 1 FROM ledger_entry l WHERE l.claim_id = c.id AND l.kind = 'provisional');

-- Settled with no settle row (lost by the old two-step settle). Not repaired automatically;
-- note the ids for an operator decision.
SELECT c.id FROM claim c WHERE c.status = 'settled'
  AND NOT EXISTS (SELECT 1 FROM ledger_entry l WHERE l.claim_id = c.id AND l.kind = 'settle');
```

The (b) and scoop rows are a one-time burst: every one is paid in the epoch current at the restart, so that epoch's points jump by their sum and the burst stays in the window for its length. If the count is large, decide before the restart whether these claims should be paid: the release has no switch to skip them.

**Check-budget preview.** The check budget switches to the new coldkey scoring (last 30 days, start 0.50, half-life 20; the old one used all history, start 0.30, half-life 50). To see which coldkeys' budget multiplier changes, run before the migration (read-only):

```bash
psql "$DATABASE_URL" -X -A -F, -t -f scripts/preview_budget.sql > outcomes.csv
python scripts/preview_budget.py outcomes.csv
```

## 2. Migrate (old API still running)

```bash
.venv/bin/prisma migrate deploy
.venv/bin/prisma migrate diff --from-url "$DATABASE_URL" \
  --to-schema-datamodel prisma/schema.prisma --script    # should print no changes
```

```sql
SELECT indisvalid FROM pg_index WHERE indexrelid = 'idx_claim_decided'::regclass;   -- t
```

If it fails:

- **Column migration, "could not lock claim in 10 attempts":** nothing changed. End the long transaction (query above), then
  `.venv/bin/prisma migrate resolve --rolled-back 20261117000000_claim_decided_at` and deploy again.
- **Index migration** (cancelled, timed out, deadlock): it leaves an INVALID `idx_claim_decided`. Drop it, mark the migration rolled back, deploy again:
  ```bash
  psql "$DATABASE_URL" -c 'DROP INDEX CONCURRENTLY IF EXISTS "idx_claim_decided"'
  .venv/bin/prisma migrate resolve --rolled-back 20261117000200_claim_decided_index
  .venv/bin/prisma migrate deploy
  ```
  If the role has a `statement_timeout` you can't change, build the index by hand instead and record it:
  ```bash
  psql "$DATABASE_URL" -c 'SET statement_timeout = 0' -c 'SET lock_timeout = 0' \
    -c 'CREATE INDEX CONCURRENTLY "idx_claim_decided" ON "claim"("decided_at", "id")'
  .venv/bin/prisma migrate resolve --applied 20261117000200_claim_decided_index
  ```

## 3. Backfill, first pass (old API still running)

```bash
.venv/bin/python scripts/backfill_decided_at.py
```

It prints progress every 5 s. Ctrl-C stops it after rolling back the batch in hand and prints `resume with --from-id N`; running it again from the start is also fine, only slower. Options: `--batch` (ids per transaction), `--pause` (seconds between batches), `--vacuum-every` (0 turns the VACUUM off).

Doing this before the restart means the new API counts the history from its first request.

## 4. Restart: the writer, then the readers one at a time

```bash
pm2 restart <writer>          # API_ROLE=writer; check /health and its log before going on
pm2 restart <reader-1>        # then each reader in turn, checking /health after each
```

The restart may stop the old writer part way through signing an epoch. That is safe: the new writer signs the epoch itself and replaces any carry rows the old one wrote. The migrations must be applied first (step 2): without the trigger change, that replacement fails and the epoch stays unsigned (503) until they are.

Never `pm2 restart all`: every process would stop at once (no reader serving while the writer starts), and a bad build would take all of them down together. The writer goes first because it is the only process that decides claims: from its restart on, every decision is stamped.

## 5. Backfill, second pass

Claims the old writer decided after the first pass reached them have no `decided_at`. Run the script again right after the writer restart; it fills only those.

```bash
.venv/bin/python scripts/backfill_decided_at.py
.venv/bin/python scripts/backfill_decided_at.py --check    # exit 0: nothing left to fill
```

Between the writer restart and the end of this pass, `GET /ledger/quality` leaves out those few claims. Each epoch's answer is cached for 3 h, so validators still agree on it. To leave none out, stop the writer for this pass instead (`pm2 stop <writer>`, run the script, `pm2 start <writer>`): about 20 s per million claims here.

## 6. Catalogue categories (right after the restart)

This release corrects outlets' categories in `app/data/catalogue.csv` (axios.com, fiercebiotech.com, endpoints.news and others gain the categories they cover), and a scoop's confirming page now counts at its source's trust in the scoop type's category or a category the type shares (`SHARED_CATEGORY` in `app/services/scoops.py`), no longer at the source's best category. `seed` skips registered sources, so the corrected categories reach the database only through `trust.py catalogue`. Until it runs, those outlets' pages count at the fallback base (0.1) in their new categories and lose confirmations they got under the old best-category rule.

```bash
.venv/bin/python scripts/trust.py catalogue --reason "catalogue categories, <release>"           # dry run: lists what it would add
.venv/bin/python scripts/trust.py catalogue --reason "catalogue categories, <release>" --apply
.venv/bin/python scripts/trust.py catalogue --reason "check" | tail -1                           # 0 missing categories to add
```

It only adds categories that have no trust row (at 0.7); a base an operator set is never changed. Read the dry run before `--apply`: each line names the source and category. It runs against the database, so run it once, from one host.

## 7. Checks

- `backfill_decided_at.py --check` prints `0 claims still to fill`. Its `no time to fill` column counts decided claims with neither a judge time nor a provisional ledger row; they stay NULL and quality does not count them.
- The change feed did not replay. Only claims whose status changed since the deploy started should have a newer `updated_at`:
  ```sql
  SELECT count(*) FROM claim WHERE updated_at > '<deploy start, UTC>';
  ```
  This should be about the number of claims received or moved since, not the number of decided claims.
- `idx_claim_decided` is valid (step 2) and the drift check prints nothing.
- `/dashboard/quality` loads on each reader.
- Once the next epoch is signed, the carry query (step 1) lists nothing, or only the epoch being signed at that moment. A `replaced K carry rows left unsigned` line in the writer's log means the restart stopped an old signing and the new writer repaired it.

## Once quality is on

Quality turns on when the subnet release sets `quality.QUALITY_FROM` (validators and the API host's subnet checkout read the same constant). From then on, each epoch's carry is valued with the cut served by `GET /ledger/quality`, and the cut is fixed by `decided_at`.

- **Never backfill `decided_at` or change it after `QUALITY_FROM` is set.** The backfill stamps past decisions with their judge or pay time. If it ran after an epoch was signed, a claim could move into that epoch's cut: the answer for an epoch validators already scored would change, and carry valued from it would no longer match. Finish the backfill (step 5, `--check` exits 0) before the release that sets `QUALITY_FROM`.
- **Never roll the API back past this release once `QUALITY_FROM` is set.** The old code stamps nothing, so claims it decides drop out of quality, and it signs without a seal, so an epoch's cut is no longer fixed. Validators would score differently from the API.
- **Validators on 4.1.2 fail every carry epoch once quality is on.** They value carry from raw points, the API from quality locks, so their `_carry_ok` check fails for every epoch with carry. Every validator must run the release with the same `QUALITY_FROM` before that time. The API host's subnet checkout must carry that same release: an API whose `QUALITY_FROM` differs from the validators' writes carry they reject.

## Rollback

Do not roll back once `QUALITY_FROM` is set (above). The writer goes first and is stopped, not restarted: its unsigned seals must be gone before the old writer starts.

1. Stop the new writer: `pm2 stop <writer>`. The readers keep serving (they never sign or write the ledger); claims and validators' ledger requests fail until step 3, so do steps 1 to 3 together.
2. With the writer stopped, delete its unsigned seals, from the new checkout:
   ```bash
   .venv/bin/python scripts/clear_seals.py            # dry run: lists them (usually none, at most one)
   .venv/bin/python scripts/clear_seals.py --apply
   ```
   This is `DELETE FROM attestation WHERE validator_hotkey = 'event-ledger' AND capacity IS NULL AND merkle_root = '' AND signature = ''`, run under the ledger lock. The old code cannot fill a seal: it would sign that epoch again on every request (a new root each time), write rows into it, and sign the next epoch past it; deleted after that, the epoch can never be signed (410). With the seal gone, the old writer signs the epoch itself, with the carry rows the seal wrote.
3. Deploy the old code (both checkouts and `prisma generate`, as in step 0) and `pm2 start <writer>`. Leave the schema as it is. The old code ignores the column, the index and the trigger change, and the trigger still stamps `updated_at` on every status change it makes. Don't drop the column: the trigger function reads `decided_at`, so every claim update would fail until it was restored.
4. Restart the readers one at a time.
5. While the old writer runs, the claims it decides get no `decided_at`, and a claim it moves back out of a decided status keeps its old one. Before rolling forward again, clear those (the trigger keeps `updated_at`):
   ```sql
   UPDATE claim SET decided_at = NULL
   WHERE decided_at IS NOT NULL AND status NOT IN ('rejected', 'provisional', 'settled', 'clawed_back');
   ```
   Then roll forward again from step 1: pre-checks, backfill, restart the writer and the readers, backfill again.
