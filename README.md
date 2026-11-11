# Alpharidge coordination API

The central service for Subnet 45's event lane. It receives signed claims from miners, stamps receipt time, decides novelty, fetches and checks each claim's source, scores source trust, and writes an append-only points ledger. The page judge is run by the listed validators: the API queues each checked claim, they post their signed answers, and the API combines them into the stored verdict. Validators pull signed points and post re-check results. Product and dashboard endpoints serve the verified events.

This README is for operators. Miners and validators should read the subnet repo's `docs/miner.md` and `docs/validator.md`.

## Contents

- [Layout](#layout)
- [Setup](#setup)
- [Configuration](#configuration)
- [Database and migrations](#database-and-migrations)
- [Running](#running)
- [Background jobs](#background-jobs)
- [Operator tools](#operator-tools)
- [Answer-key adapters](#answer-key-adapters)
- [Endpoints](#endpoints)
- [Tests](#tests)

## Layout

```
app/
├── main.py            # FastAPI app, middleware, start-up tasks
├── config.py          # Every setting, read from the environment
├── routers/           # claims, ledger, scoops, product, dashboard, ops, mechanism, config, …
├── services/          # intake, line, novelty, verifier, judge, trust, resolve, scoops, points,
│                      # ledger, ops, product, answer_key/, store (Prisma + in-memory)
├── jobs/              # worker, trust nightly, answer key
├── utils/             # request auth, receipt clock, block clock, rate limits, signing
└── data/              # trust seed, outlet catalogue, scan feeds
prisma/                # schema.prisma and migrations
scripts/               # trust, seed_entities, api_keys, publish_profile
```

The subnet package (`alpharidge_ai`, from the subnet repo) is installed with its `[verify]` extra. Its `schema` and `verify` modules hold the event-type table, the entity seed, the fetch rules and the claim check, so the API and validators reach the same verdict on the same page.

## Setup

Needs Python 3.10+, PostgreSQL 15+ with the `pgvector` extension available, and a checkout of the subnet repo.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -e "../alpharidge-ai[verify]"   # path to the subnet repo
.venv/bin/prisma generate
cp .env.example .env                                            # then fill it in
```

`requirements.txt` is the runtime set; `requirements-dev.txt` adds pytest.

### Attestation key

The ledger is signed with an sr25519 key from `API_ATTESTATION_PRIVKEY`, a 32-byte hex seed. Validators pin the matching public key (`API_ATTESTATION_PUBKEY` in the subnet's `config.py`), so a new key means a subnet release. To make one:

```bash
.venv/bin/python -c "import secrets; from bittensor_wallet import Keypair; s = secrets.token_hex(32); print(s); print(Keypair.create_from_seed('0x' + s).ss58_address)"
```

Keep the seed secret and out of the repo.

## Configuration

Everything is read from the environment, with `.env` in the repo root loaded at start-up. Defaults live in `app/config.py`. Names only here; see that file for defaults.

**Core**

| Key | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (used by Prisma) |
| `API_HOST`, `API_PORT`, `API_RELOAD` | Server bind |
| `API_ATTESTATION_PRIVKEY` | Ledger and profile signing seed (secret) |
| `BT_NETWORK` / `SUBTENSOR_NETWORK`, `SUBNET_UID` | Chain to read the metagraph from; `mock` runs a wall-clock chain for local tests |
| `MOCK_BLOCK_SECONDS`, `MOCK_GENESIS_TS` | Mock chain clock (local tests only) |
| `AUTH_ENABLED`, `AUTH_SIGNATURE_TIMEOUT`, `AUTH_NONCES_PER_HOTKEY` | Request signing |
| `STAKE_THRESHOLD` | Stake a permit holder needs to count as a validator |
| `OPERATOR_HOTKEYS` | Hotkeys allowed on `/ops/*` |
| `REVERIFY_VALIDATORS` | Validators whose re-verify disagreements hold a claim for operator review (other validators' results are recorded only), and who see open scoops and send pages that confirm them |
| `BLOCKED_HOTKEYS` | Hotkeys whose requests are dropped before authentication |
| `ALLOW_MANUAL_HOTKEYS`, `MANUAL_MINER_HOTKEYS`, `MANUAL_VALIDATOR_HOTKEYS`, `MOCK_COLDKEYS` | Fixed hotkey lists and `hotkey:coldkey` pairs (local tests) |
| `INTERNAL_ALLOWED_IPS` | Extra peers for internal-only routes |
| `PRODUCT_API_KEYS` | Product API keys from the environment (more live in the `api_key` table) |
| `MIN_VALIDATOR_VERSION`, `VALIDATOR_VERSIONS_PATH` | Validator version gate |
| `VALIDATOR_EL_JUDGE_URL`, `VALIDATOR_EL_JUDGE_MODEL` | The page judge's endpoint and model, served to `REVERIFY_VALIDATORS` on `/config/subnet` (each validator uses its own key) |
| `VALIDATOR_REVERIFY_FETCH_TIMEOUT_S`, `VALIDATOR_REVERIFY_DOMAIN_RPS`, `VALIDATOR_SCAN_INTERVAL_S`, `VALIDATOR_JUDGE_POLL_S` | Optional validator settings served on `/config/subnet`, with `EL_USER_AGENT_CONTACT`; a validator's `OVERRIDE_<KEY>` wins |

**Event lane** (`EL_*`)

| Keys | Purpose |
|---|---|
| `EL_TYPES` | Event types accepted at intake (default: every open type in the subnet's `types.json`, in four categories: finance, technology, politics, sports); others are rejected as `type_off`. The category comes from the type; there is no per-category pool |
| `EL_SCOOPS` | An untrusted page can be held for up to 72 h as a scoop until a trusted page confirms it (default `true`; `false` turns scoops off) |
| `EL_SCAN_FEEDS` | The feed list sent to listed validators with the open scoops (default `app/data/scan_feeds.json`) |
| `EL_CHECK_BUDGET_BASE` | Daily checks per hotkey before verified claims add to it |
| `EL_RATE_LIMIT_ENABLED` | Per-IP rate limits (behind a reverse proxy, pass the client address through) |
| `EL_FETCH_TIMEOUT_S`, `EL_FETCH_RETRY`, `EL_FETCH_PUBLIC_ONLY`, `EL_DOMAIN_RPS`, `EL_FETCH_CONCURRENCY`, `EL_USER_AGENT_CONTACT` | Fetching (15 s a request, 5 s to connect, as validators); the user agent is `AlphaRidgeBot/<version> (+<contact>)`. The contact is a URL or email the operator chooses |
| `EL_SETTLE_DELAY_H`, `EL_SETTLE_EVERY_S`, `EL_WORKER_INTERVAL_S` | Settlement and worker timing (a signed profile's `ledger.settle_delay_h` overrides `EL_SETTLE_DELAY_H`) |
| `API_ROLE` | `writer` (default) runs everything. `reader` serves only GET `/health`, `/claims/{id}`, `/events`, `/entities`, `/feed` and `/dashboard/*`, runs no jobs and writes nothing; run several readers on their own ports and route those paths to them. Run exactly one writer |
| `EL_PARSE_PROCESSES` | Worker processes that parse fetched pages (default 0: one thread in the API process). Parsing is CPU-bound; a few processes take it off the API's event loop |
| `EL_TIE_NETWORK` | Chain endpoint for tie-block lookups, such as `archive`; they read block timestamps from the past, which a pruned node no longer keeps. Unset uses the main connection |
| `EL_HOST_RPS` | Per-host request rates, `host=rate,...` (e.g. `www.prnewswire.com=0.5`); other hosts use `EL_DOMAIN_RPS`. A host that answers 429 is left alone for its Retry-After (60 s without one) and the fetch is tried again later |
| `EL_STATUS_CACHE_S` | Seconds a claim's `GET /claims/{id}` reply is reused (default 20; 0 reads it fresh each time) |
| `EL_TICK_LOG_S`, `EL_PROFILE_S` | Diagnostics, off by default: log worker ticks slower than this many seconds by step; profile the process for this many seconds after start-up and log the result |
| `EL_JUDGE_HOTKEY_ITEMS`, `EL_JUDGE_NEW_HOTKEY_ITEMS` | Claims a hotkey may send to the page judge per UTC day (2666), and with no passing claim in 7 days (266); past it a claim is `judge_allowance`. The API runs no model and holds no judge key: the listed validators judge pages with their own keys, and a claim waits until they answer (`judge_failing` when none has within 20 minutes; with no answer for 24 h it is `judge_unavailable`, which uses none of the check budget) |
| `EL_RECHECK_KEY`, `EL_RECHECK_PROXY`, `EL_RECHECK_PROXY_PUBLIC_ONLY` | Later re-check of paid sources (the key is secret) |
| `EL_RECHECK_*`, `EL_PAGE_FLAG_*`, `EL_ALARM_CLAW_*`, `EL_ALARM_COLDKEY_*`, `EL_ALARM_FETCH_*`, `EL_AUDIT_*`, `EL_HEDGE_GAP_S`, `EL_TRUST_SPIKE_*` (the names in `.env.example`) | Re-check sampling and timing, page-type review thresholds, the ops alarms (clawed share, coldkey concentration, fetch failures), the judge audit bar, the hedge-pair gap and the source volume spike flag. No public default: the API won't start until each is set |
| `EL_AK_ADAPTERS`, `EL_AK_SEC_USER_AGENT`, `EL_AK_BLS_URL`, `EL_AK_BLS_CALENDAR_URL` | Answer-key adapters |
| `EL_REGISTRY`, `EL_ENTITY_OVERRIDES` | Entity registry lookups (Wikidata, SEC, GLEIF; on by default, but only while `EL_USER_AGENT_CONTACT` is set) and the entity overrides file |
| `EL_PUBLISH_HOLD_S`, `EL_PRODUCT_KEYS_REQUIRED`, `EL_API_UNVERIFIED` | Product publishing hold, access and unverified labels |

Scoring, identity and trust values are constants in the code that uses them. The ledger window and capacity come from the signed mechanism profile (schema 2.0.0: `settlement.C` and the `ledger` section, parsed with the subnet's own parser); without one, the subnet's `mechanism.pools` constants are used. The API imports these, the close grace, the ledger hashing and the check settings from the subnet package, so validators reach the same result.

## Database and migrations

Migrations live in `prisma/migrations`: a `baseline`, then the event-lane migrations. The event-lane migration runs `CREATE EXTENSION vector`, so the database role needs rights to create it (or create it once as a superuser beforehand).

**A new database:**

```bash
.venv/bin/prisma migrate deploy
```

**A database that already has the baseline tables:** mark the baseline as applied, then deploy the rest. Rehearse on a copy first.

```bash
.venv/bin/prisma migrate resolve --applied 20261013000000_baseline
.venv/bin/prisma migrate deploy
.venv/bin/prisma migrate diff --from-url "$DATABASE_URL" \
  --to-schema-datamodel prisma/schema.prisma --script    # should print no changes
```

The baseline's tables outside the event lane are kept as they are; no migration drops them.

Two pieces are hand-written SQL that Prisma can't express: the HNSW index on `events.embedding` and the append-only trigger on `ledger_entry`. When you generate a new migration with `prisma migrate dev`, delete the `DROP INDEX` Prisma adds for the HNSW index before committing it.

**Seed data** (once, after migrating):

```bash
.venv/bin/python scripts/seed_entities.py load      # entity aliases from the subnet's schema/data/entities.json
.venv/bin/python scripts/trust.py seed              # official sources and page rules from app/data/trust_seed.json
```

The entity seed is a data file in the subnet repo; edit it there.

## Running

```bash
./run_api.sh          # same as: .venv/bin/python -m app
```

One process serves the HTTP API and runs every background job. Run a single instance: rate limits and job schedules are kept per process. Put it behind a reverse proxy that forwards the client address (uvicorn's proxy headers), since rate limits are per IP. The host clock must be NTP-synced, because receipt time is stamped from it.

## Background jobs

All start with the app (`app/main.py` lifespan):

| Job | Schedule | What it does |
|---|---|---|
| Worker (`jobs/worker.py`) | every `EL_WORKER_INTERVAL_S` | Runs novelty on claims whose bucket has closed, line resolution, settlement every `EL_SETTLE_EVERY_S`, and expiry of claims that waited too long. Fetch, re-check and report jobs run beside it in a pool of `EL_FETCH_CONCURRENCY` tasks; a report job fetches a trusted claim on an event another claim took and, when its page passes the fixed check, counts its owner toward the event's `confirmed` label (no judge, no pay) |
| Trust nightly (`jobs/trust_nightly.py`) | daily at 03:00 UTC | Recomputes source trust and bands, writes page-type review flags to the trust log |
| Answer key (`jobs/answer_key.py`) | each adapter at its own interval | Polls official feeds, matches records to events, updates per-category accuracy |
| Whitelist sync | every 2 minutes | Refreshes miner and validator hotkeys from the metagraph |

`.venv/bin/python scripts/trust.py nightly` runs the trust job once by hand.

## Operator tools

### Trust registry (`scripts/trust.py`)

Every change is written to the trust log with a reason and the operator's name.

```bash
.venv/bin/python scripts/trust.py seed [--catalogue export.csv]        # load the seed, optionally a catalogue
.venv/bin/python scripts/trust.py add example.com/ --trust 0.7 [--category finance] --reason "..."
.venv/bin/python scripts/trust.py set example.com --trust 0.8 --category finance --reason "..."
.venv/bin/python scripts/trust.py ban example.com --reason "..."
.venv/bin/python scripts/trust.py unban example.com --reason "..."
.venv/bin/python scripts/trust.py rule "example.com/blogs/*" --kind exclude|allow|listing|wire --reason "..."
.venv/bin/python scripts/trust.py why example.com                      # how a source's trust was reached
.venv/bin/python scripts/trust.py review [--days 7]                    # page-type review flags
.venv/bin/python scripts/trust.py nightly
```

The catalogue import takes a `domain,path,categories` CSV, such as a Media Cloud collection export (needs a Media Cloud account); `app/data/catalogue.csv` is the outlet list. Sources are per host: `domain` is a host, and `*.host` also covers its subdomains. Rows on shared hosting platforms (`trust.TRUST_SHARED_DOMAINS`) are skipped. Re-run it monthly. `app/data/trust_seed.json` holds the official sources and page rules. An official row with a `subject` (`{"ids": ["wikidata:Q312"], "names": ["Apple"]}`), such as a company newsroom or a club site, is official only for claims that name that subject among their parties; other claims on it count at the outlet base trust. Rows without one (a government, a league) speak for any claim. Seed rows and catalogue rows carry the categories they are trusted for; a subject comes only from the seed, so a row added with `trust.py add` has none. `seed` skips sources already registered, so a row changed in the seed or catalogue reaches an existing database only through `trust.py set` (or a fresh database). Release wires are never official enough to settle a claim on their own.

### Product API keys (`scripts/api_keys.py`)

```bash
.venv/bin/python scripts/api_keys.py add <owner>     # prints the key once; only its sha256 is stored
```

Clients send it as `X-API-Key`. `EL_PRODUCT_KEYS_REQUIRED=false` opens product reads to the public.

### Mechanism profile (`scripts/publish_profile.py`)

```bash
.venv/bin/python scripts/publish_profile.py --from profile.json --activate-in 7d --dry-run
.venv/bin/python scripts/publish_profile.py --from profile.json --activate-in 7d
```

Signs and appends a new profile; there is no edit or delete. Run it dry first.

### Operator routes

Signed by a hotkey in `OPERATOR_HOTKEYS`: `GET /ops/alarms` (held claims, fetch failures, concentration, coldkeys with a high clawed share of recent paid claims, ledger rows in a category that is not open, answer-key gaps, ledger staleness, missed re-verify reports, retraction notices and changed pages seen on re-fetch, trusted contradictions, judge accuracy, judge items no listed validator answered in 20 minutes), `GET /ops/audit` (judge-passed claims an official feed matched: agreement, its 95% lower bound and the disagreements), `GET /ops/trust` (recent band changes, bans and new sources), `GET /ops/hedges?days=` (with scoops on: claims on one event that fill its deciding slot differently) and `POST /ops/claims/{id}/resolve` with `{"action": "claw"|"clear", "reason": "..."}` for a held claim, `POST /ops/claims/{id}/retract` or `/ops/events/{id}/retract` (the event's lead claim) with `{"kind": "retraction"|"correction", "slot": "...", "reason": "..."}` (a correction counts only for the actor, type, stage or a fact the claim asserted), `POST /ops/claims/{id}/penalise` with `{"reason": "..."}` for confirmed misconduct (the claw while the claim is still clawable, and a penalty of 5 times the claim's full value), `GET /ops/wrong?days=&people=` (claims, wrong claims, and points earned, clawed and penalised, per hotkey and stored trust band), and `PUT` / `DELETE /ops/blacklist/{hotkey}` (`{"reason": "..."}` on `PUT`) to block or unblock a hotkey at claim intake.

## Answer-key adapters

Official feeds polled privately. They feed per-category accuracy, the judge audit, release times, Fed/BLS wrong-claim findings and the product's official-match flag, and never create events or appear in public responses. `EL_AK_ADAPTERS` lists the enabled ones (unset: edgar, fed, bls, bea; set but empty: none).

| Adapter | Source | Default | Needs |
|---|---|---|---|
| `edgar` | SEC EDGAR full-text search (8-K, S-1, 13D/13G) | on, but skipped until configured | `EL_AK_SEC_USER_AGENT` naming the operator with a contact email, as the SEC requires |
| `fed` | Federal Reserve monetary policy press feed; values, can prove a claim wrong | on | — |
| `bls` | BLS latest-releases feed and release calendar; values, can prove a claim wrong | on | — |
| `bea` | BEA release schedule, for release times only | on | — |

EDGAR records are identity only and count toward accuracy; they never prove a claim wrong.

## Other keys the operator provides

| Feature | Keys | Without them |
|---|---|---|
| Registry lookups (`EL_REGISTRY=http`, the default) | `EL_USER_AGENT_CONTACT`, `EL_AK_SEC_USER_AGENT` for CIKs | Lookups stay off: only the entity seed and `app/data/entity_overrides.json` (or the file `EL_ENTITY_OVERRIDES` names) are used, and politicians and athletes outside the seed stay labelled unverified until confirmed |
| Ledger signing | `API_ATTESTATION_PRIVKEY` | `/ledger/points` can't sign |
| Product access | `PRODUCT_API_KEYS` or `scripts/api_keys.py` | Product reads need a signed miner or validator request |
| Catalogue trust | A Media Cloud export | Only the seed's official sources start trusted |

## Endpoints

| Area | Routes | Caller |
|---|---|---|
| Claims | `POST /claims`, `GET /claims/{id}` | Miner (signed) |
| Ledger | `GET /ledger/points`, `/ledger/leaves`, `/ledger/sample`, `POST /ledger/reverify` | Validator (signed) |
| Scoops | `GET /scoops/open`, `POST /scoops/confirm` | Listed validator (signed) |
| Page judge | `GET /judge/pending`, `POST /judge/verdicts` | Listed validator (signed) |
| Product | `GET /events`, `/events/{id}`, `/entities/{id}`, `/feed` | API key, or signed miner/validator |
| Dashboard | `GET /dashboard/miners`, `/dashboard/items`, `/dashboard/validators`, `/dashboard/mechanism`, `/dashboard/sources` | Public, rate-limited |
| Operator | `/ops/*` | `OPERATOR_HOTKEYS` |
| Mechanism | `GET /mechanism/profile` | Validator |
| Config | `GET /config/subnet`: served validator settings and the minimum validator version | Validator (signed) |
| Misc | `/health`, `/validators/versions` (internal), and `/v2` (410 Gone) | — |

`GET /dashboard/mechanism` gives the capacity `C` in force, the open categories, the window, the epoch length and the last signed total. `GET /dashboard/sources` is the trusted list: each source whose pages can win, with its trust per category, its kind (`official`, `outlet`, `wire`), `official_for` on a company's or club's own site, and the page rules (excluded and listing pages) that apply on any source.

Events about people who aren't linked public figures, and legal or regulatory claims about any person, stay `unverified` until trusted, independent sources confirm them. Until then they are not on `/events` or `/feed` by default; the public dashboard lists them with `verification: "unverified"`, and API-key callers can request them with `include_unverified=true` while `EL_API_UNVERIFIED` is on (the default). An event whose claim is later contradicted, retracted or corrected carries `marked`. A paid claim proved wrong (an official figure contradicts it, the operator retracts or corrects it, or an official page settles it otherwise) is clawed back, in every trust band. A paid claim whose source page later changes or disappears can be clawed back. A trusted outlet that disagrees only raises an operator alarm.

Each `/dashboard/items` row carries `validators`: one entry per validator hotkey that judged the claim's page, re-checked it or found a page for it, `{"hotkey", "verdict": "pass"|"reject"|"unclear"|null, "reason", "recheck": "agree"|"disagree"|"unreachable"|null, "confirm": {"status", "reason"}|null}`. `verdict` is that validator's own reading of its latest posted answers and `reason` the code miners get (`fixed_check`, `judge_reject`, `judge_unclear`); no scores or answers. `recheck` is its `/ledger/reverify` verdict (an agree on a page that has since changed counts as agree). `confirm` is its best found page for a scoop (confirmed, then queued, then rejected) with the coarse reason, never the page. Like `source_domain`, the list is shown only to the claim's own hotkey and operators until the claim settles, and is `null` to everyone else before then.

`GET /dashboard/validators?window=24h|7d` (default `7d`) lists each validator hotkey that posted a page verdict or a re-check report in the window: `verdicts` and `rechecks` counted on claims that are open to everyone, `verdict_agreement` and `recheck_agreement` (the share of items where its outcome equals the most common outcome of the other validators on the item, leaving out items no other validator answered or where they tie; agree and disagree only for re-checks), `last_judged_epoch`, `last_rechecked_epoch`, `missed_epochs` (for listed re-verifiers: epochs with paid claims past the 24 h report deadline it sent no report for; `null` for others) and `last_weights_epoch` (always `null`: the API does not see weights).

Signed requests use the subnet's per-request scheme (`X-Auth-*` headers over method, path, query, body hash, time and nonce).

Every stored claim gets a receipt signed with `API_ATTESTATION_PRIVKEY`, the ledger key (`alpharidge_ai.ledger.receipts`): returned by `POST /claims` (the same one on a re-send) and shown on `GET /claims/{id}` and `/dashboard/items`. Receipts are written in the claim's transaction, each naming the previous one's hash, in the append-only `claim_receipt` table.

`GET /claims?since=<cursor>&limit=` (signed by the miner) returns the caller's claims that changed after the cursor, each with its status reply, and the next cursor; one call replaces polling each open claim.

`POST /claims` answers `429` with `budget_exhausted` or `rate_limited` and a `Retry-After` header, and stores nothing. Actor IDs in a claim (Wikidata QID, CIK, LEI) link only when the registry record's name or an alias matches the actor's name; with `EL_REGISTRY=off` only the entity seed and overrides are known.

## Tests

```bash
.venv/bin/python -m pytest -q tests
```

The suite runs without a database or network (in-memory store and saved fixtures). `tests/test_guards.py` holds the mechanism guard tests and `tests/test_replays.py` the novelty replays. For a full local run with a real database, a mock chain, the reference miner and a validator, see `scripts/e2e_local.sh` in the subnet repo.
