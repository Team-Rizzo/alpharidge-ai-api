"""Prisma store for production."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.services.entities import canonical_id, record_from, refreshes
from app.utils.clock import ms as _ms
from app.services.store.rows import (
    INTAKE_REJECTS, PASSED, PRIZE_ROUTES, SCOOP_MISSES, WON, AccuracyCheckRow, AnswerKeyRow,
    ClaimHead, ClaimRow, DuplicateClaim, DuplicateEvent, EventFactRow, EventRecord, FeedRow, HotkeyStats,
    Job, JudgeItemRow, JudgePostRow, LedgerRow, MinerAccuracyRow, MinerStats, OwnerRow, PageRuleRow, ReverifyRow,
    ScoopCandidateRow,
    SourceEventRow, SourceFetchRow, SourceRow, SourceTrustRow, TrustLogRow,
)


def _dt(ms: Optional[int]) -> Optional[datetime]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


_CLAIM_FIELDS = {
    "status": "status", "reason": "reason", "event_id": "eventId", "novelty": "novelty",
    "route": "route", "held": "held",
    "pending_of_event_id": "pendingOfEventId", "doc_hash": "docHash", "category": "category",
    "source_id": "sourceId", "trust": "trust", "trust_band": "trustBand",
    "wrong": "wrong", "event_type": "eventType",
}
_CLAIM_TIMES = {"verified_ms": "verifiedAt", "judged_ms": "judgedAt", "settle_ms": "settleAt"}


_RECEIPT_LOCK = 4545001
_RECEIPT_SQL = ('SELECT seq, version, prev, claim_id, hotkey, body_sha256, received_ms, '
                'hash, signature FROM "claim_receipt"')


def _receipt_from(r) -> dict:
    return {"v": int(r["version"]), "seq": int(r["seq"]), "prev": r["prev"],
            "claim_id": str(r["claim_id"]), "hotkey": r["hotkey"],
            "body_sha256": r["body_sha256"], "received_ms": int(r["received_ms"]),
            "hash": r["hash"], "signature": r["signature"]}


def _claim_from(m) -> ClaimRow:
    return ClaimRow(
        id=m.id, hotkey=m.hotkey, signature=m.signature, nonce=m.nonce, signed_at=m.signedAt,
        body_hash=m.bodyHash, body=m.body or "", received_ms=_ms(m.receivedAt),
        bucket=m.receiptBucket if m.receiptBucket is not None else 0, kind=m.kind,
        category=m.category, event_type=m.eventType,
        payload=m.payload if isinstance(m.payload, dict) else json.loads(m.payload),
        source_url=m.sourceUrl, status=m.status, reason=m.reason, event_id=m.eventId,
        novelty=m.novelty, route=m.route, held=m.held,
        verified_ms=_ms(m.verifiedAt), judged_ms=_ms(m.judgedAt), settle_ms=_ms(m.settleAt),
        pending_of_event_id=m.pendingOfEventId, doc_hash=m.docHash, source_id=m.sourceId,
        trust=m.trust, trust_band=m.trustBand, wrong=m.wrong,
        coldkey=m.coldkey)


_FETCH_FIELDS = {
    "verdict": "verdict", "http_status": "httpStatus", "content_hash": "contentHash",
    "final_url": "finalUrl", "canonical_url": "canonicalUrl", "bytes": "bytes",
    "page_type": "pageType", "reason": "reason",
}


def _fetch_data(fields: dict) -> dict:
    from prisma import Json

    data = {_FETCH_FIELDS[k]: v for k, v in fields.items() if k in _FETCH_FIELDS}
    if "spans" in fields:
        data["spans"] = Json(fields["spans"]) if fields["spans"] is not None else None
    if "check" in fields:
        data["checkResult"] = Json(fields["check"]) if fields["check"] is not None else None
    if "published_ms" in fields:
        data["publishedAt"] = _dt(fields["published_ms"])
    return {k: v for k, v in data.items() if v is not None}


def _fetch_from(m) -> SourceFetchRow:
    return SourceFetchRow(
        id=m.id, claim_id=m.claimId, verdict=m.verdict, http_status=m.httpStatus,
        content_hash=m.contentHash, spans=m.spans, final_url=m.finalUrl,
        canonical_url=m.canonicalUrl, published_ms=_ms(m.publishedAt),
        bytes=m.bytes,
        page_type=m.pageType, reason=m.reason, check=_json(m.checkResult) or None,
        fetched_ms=_ms(m.fetchedAt))


def _ledger_from(m) -> LedgerRow:
    return LedgerRow(id=m.id, hotkey=m.hotkey, claim_id=m.claimId, event_id=m.eventId,
                     kind=m.kind, points=m.points, epoch=m.epoch, route=m.route,
                     category=m.category, factors=m.factors, reason=m.reason,
                     received_ms=m.receivedMs, bucket=m.receiptBucket, coldkey=m.coldkey)


class PrismaStore:
    def __init__(self, client):
        self.db = client

    async def is_blacklisted(self, hotkey):
        return await self.db.blacklistedhotkey.find_unique(where={"hotkey": hotkey}) is not None

    async def set_blacklisted(self, hotkey, blocked, reason=None):
        if blocked:
            await self.db.blacklistedhotkey.upsert(
                where={"hotkey": hotkey},
                data={"create": {"hotkey": hotkey, "reason": reason}, "update": {"reason": reason}})
        else:
            await self.db.blacklistedhotkey.delete_many(where={"hotkey": hotkey})

    async def claim_by_body(self, hotkey, body_hash):
        m = await self.db.claim.find_first(where={"hotkey": hotkey, "bodyHash": body_hash})
        return _claim_from(m) if m else None

    async def create_claim(self, row, jobs=(), receipt=None):
        from prisma import Json
        from prisma.errors import UniqueViolationError

        data = {
            "hotkey": row.hotkey, "signature": row.signature, "nonce": row.nonce,
            "signedAt": row.signed_at, "bodyHash": row.body_hash, "body": row.body,
            "receivedAt": _dt(row.received_ms), "receiptBucket": row.bucket, "kind": row.kind,
            "category": row.category, "eventType": row.event_type, "payload": Json(row.payload),
            "sourceUrl": row.source_url, "status": row.status, "reason": row.reason,
            "trust": row.trust, "trustBand": row.trust_band, "coldkey": row.coldkey,
        }
        try:
            async with self.db.tx() as tx:
                prev = None
                if receipt is not None:
                    # One receipt sequence: appends wait for each other until commit.
                    await tx.query_raw(
                        f"SELECT 1 AS ok FROM pg_advisory_xact_lock({_RECEIPT_LOCK})")
                    rows = await tx.query_raw(_RECEIPT_SQL + " ORDER BY seq DESC LIMIT 1")
                    prev = _receipt_from(rows[0]) if rows else None
                m = await tx.claim.create(data=data)
                for kind in jobs:
                    await tx.job.create(data={"kind": kind, "claimId": m.id})
                if receipt is not None:
                    r = receipt(int(m.id), prev)
                    await tx.execute_raw(
                        'INSERT INTO "claim_receipt" (seq, version, prev, claim_id, hotkey, '
                        'body_sha256, received_ms, hash, signature) VALUES '
                        '($1::bigint, $2::int, $3, $4::bigint, $5, $6, $7::bigint, $8, $9)',
                        r["seq"], r["v"], r["prev"], int(r["claim_id"]), r["hotkey"],
                        r["body_sha256"], r["received_ms"], r["hash"], r["signature"])
        except UniqueViolationError as e:
            raise DuplicateClaim(row.hotkey) from e
        return _claim_from(m)

    async def receipt_of(self, claim_id):
        rows = await self.db.query_raw(_RECEIPT_SQL + " WHERE claim_id = $1::bigint",
                                       int(claim_id))
        return _receipt_from(rows[0]) if rows else None

    async def receipts_for(self, claim_ids):
        ids = [int(i) for i in claim_ids]
        if not ids:
            return {}
        rows = await self.db.query_raw(_RECEIPT_SQL + " WHERE claim_id = ANY($1::bigint[])", ids)
        return {int(r["claim_id"]): _receipt_from(r) for r in rows}

    async def get_claim(self, claim_id):
        m = await self.db.claim.find_unique(where={"id": claim_id})
        return _claim_from(m) if m else None

    async def update_claim(self, claim_id, expect, **fields):
        data = {_CLAIM_FIELDS[k]: v for k, v in fields.items() if k in _CLAIM_FIELDS}
        data.update({_CLAIM_TIMES[k]: _dt(v) for k, v in fields.items() if k in _CLAIM_TIMES})
        if "payload" in fields:
            from prisma import Json
            data["payload"] = Json(fields["payload"])
        n = await self.db.claim.update_many(where={"id": claim_id, "status": expect}, data=data)
        return n > 0

    async def claims_by_status(self, status, received_before_ms=None, limit=500, skip=()):
        where = {"status": status}
        if skip:
            where["id"] = {"not_in": [int(i) for i in skip]}
        if received_before_ms is not None:
            where["receivedAt"] = {"lt": _dt(received_before_ms)}
        rows = await self.db.claim.find_many(
            where=where, order=[{"receiptBucket": "asc"}, {"id": "asc"}], take=limit)
        return [_claim_from(m) for m in rows]

    async def judge_repeats(self, owner, event_type, doc_hash, since_ms, judged_since_ms,
                            exclude_id, reasons):
        rows = await self.db.query_raw(
            'SELECT count(*) AS n FROM "claim" WHERE event_type = $1 '
            'AND received_at >= to_timestamp($2::double precision / 1000) AND id <> $3::bigint '
            'AND COALESCE(coldkey, hotkey) = $4 AND doc_hash = $5 '
            'AND judged_at >= to_timestamp($6::double precision / 1000) '
            "AND reason = ANY(string_to_array($7, ','))",
            event_type, since_ms, exclude_id, owner, doc_hash, judged_since_ms, ",".join(reasons))
        return int(rows[0]["n"]) if rows else 0

    async def claim_changes(self, hotkey, after, until_ms, limit):
        rows = await self.db.query_raw(
            'SELECT id, ms FROM (SELECT id, (extract(epoch FROM updated_at) * 1000)::bigint AS ms '
            'FROM "claim" WHERE hotkey = $1 AND updated_at >= to_timestamp($2::double precision '
            '/ 1000) - interval \'1 millisecond\' AND updated_at < to_timestamp($4::double '
            'precision / 1000)) c WHERE (ms, id) > ($2::bigint, $3::bigint) '
            'ORDER BY ms, id LIMIT $5',
            hotkey, int(after[0]), int(after[1]), int(until_ms), int(limit))
        return [(int(r["id"]), int(r["ms"])) for r in rows]

    async def claim_heads(self, status, limit=500, skip=()):
        rows = await self.db.query_raw(
            'SELECT id, event_id, source_url, '
            '(extract(epoch FROM received_at) * 1000)::bigint AS received_ms, '
            "payload->>'event_date' AS event_date FROM \"claim\" WHERE status = $1 "
            "AND id <> ALL(string_to_array($2, ',')::bigint[]) "
            'ORDER BY receipt_bucket, id LIMIT $3',
            status, ",".join(str(int(i)) for i in skip), limit)
        return [ClaimHead(int(r["id"]), int(r["event_id"]) if r["event_id"] is not None else None,
                          r["source_url"], int(r["received_ms"]), r["event_date"]) for r in rows]

    async def claims_ahead(self, claim, since_ms):
        at = _dt(claim.received_ms)
        return await self.db.claim.count(where={
            "hotkey": claim.hotkey,
            "receivedAt": {"gte": _dt(since_ms)},
            "AND": [
                {"OR": [{"receivedAt": {"lt": at}}, {"receivedAt": at, "id": {"lt": claim.id}}]},
                {"OR": [{"reason": None}, {"reason": {"not_in": list(INTAKE_REJECTS)}}]},
            ],
        })

    async def budget_use(self, hotkey, since_ms):
        rows = await self.db.query_raw(
            'SELECT count(*) AS n, (extract(epoch FROM min(received_at)) * 1000)::bigint AS first '
            'FROM "claim" WHERE hotkey = $1 AND received_at >= to_timestamp($2::double precision '
            '/ 1000) AND (reason IS NULL OR reason <> ALL(string_to_array($3, \',\')))',
            hotkey, since_ms, ",".join(INTAKE_REJECTS))
        r = rows[0] if rows else {}
        return int(r.get("n") or 0), int(r["first"]) if r.get("first") is not None else None

    async def queued_hotkeys(self):
        rows = await self.db.query_raw(
            'SELECT DISTINCT hotkey FROM "claim" WHERE status = \'queued\'')
        return [r["hotkey"] for r in rows]

    async def admissible_claims(self, limits, since_ms, limit):
        if not limits:
            return []
        rows = await self.db.query_raw(
            'SELECT q.id FROM (SELECT id, hotkey, status, receipt_bucket, row_number() OVER '
            '(PARTITION BY hotkey ORDER BY received_at, id) AS n FROM "claim" '
            'WHERE hotkey = ANY(string_to_array($1, \',\')) '
            'AND received_at >= to_timestamp($3::double precision / 1000) '
            'AND (reason IS NULL OR reason <> ALL(string_to_array($4, \',\')))) q '
            'JOIN unnest(string_to_array($1, \',\'), string_to_array($2, \',\')::int[]) '
            'AS l(hotkey, n) ON l.hotkey = q.hotkey '
            "WHERE q.status = 'queued' AND q.n <= l.n ORDER BY q.receipt_bucket, q.id LIMIT $5",
            ",".join(limits), ",".join(str(v) for v in limits.values()), since_ms,
            ",".join(INTAKE_REJECTS), limit)
        ids = [int(r["id"]) for r in rows]
        found = {m.id: m for m in await self.db.claim.find_many(where={"id": {"in": ids}})}
        return [_claim_from(found[i]) for i in ids if i in found]

    async def quality_outcomes(self, since_ms, junk, until_ms=None):
        rows = await self.db.query_raw(
            'SELECT COALESCE(coldkey, hotkey) AS owner, status = ANY(string_to_array($2, \',\')) '
            'AS passed FROM "claim" WHERE judged_at >= to_timestamp($1::double precision / 1000) '
            'AND judged_at < to_timestamp($4::double precision / 1000) '
            'AND (status = ANY(string_to_array($2, \',\')) OR (status = \'rejected\' '
            'AND reason = ANY(string_to_array($3, \',\')))) ORDER BY judged_at, id',
            since_ms, ",".join(PASSED), ",".join(junk),
            until_ms if until_ms is not None else 2 ** 52)
        return [(r["owner"], bool(r["passed"])) for r in rows]

    async def hotkey_stats(self, since_ms, hotkeys=None):
        sql = ('SELECT hotkey, count(*) FILTER (WHERE status = ANY(string_to_array($2, \',\'))) '
               'AS passes '
               'FROM "claim" WHERE judged_at >= to_timestamp($1::double precision / 1000)')
        args = [since_ms, ",".join(PASSED)]
        if hotkeys is not None:
            sql += " AND hotkey = ANY(string_to_array($3, ','))"
            args.append(",".join(hotkeys))
        rows = await self.db.query_raw(sql + " GROUP BY hotkey", *args)
        return {r["hotkey"]: HotkeyStats(int(r["passes"])) for r in rows}

    async def scoop_record(self, since_ms, owners):
        owners = list(owners)
        if not owners:
            return {}
        rows = await self.db.query_raw(
            'SELECT COALESCE(coldkey, hotkey) AS owner, count(*) FILTER (WHERE wrong IS NULL '
            'AND status = ANY(string_to_array($3, \',\'))) AS hits, count(*) AS judged '
            'FROM "claim" WHERE route = \'scoop\' '
            "AND COALESCE(coldkey, hotkey) = ANY(string_to_array($1, ',')) "
            'AND received_at >= to_timestamp($2::double precision / 1000) '
            "AND (wrong IS NOT NULL OR status = ANY(string_to_array($3, ',')) "
            "OR status = 'clawed_back' OR reason = ANY(string_to_array($4, ','))) "
            "GROUP BY COALESCE(coldkey, hotkey)",
            ",".join(owners), since_ms, ",".join(PASSED), ",".join(SCOOP_MISSES))
        return {r["owner"]: (int(r["hits"]), int(r["judged"])) for r in rows}

    async def tie_seeds(self, buckets):
        buckets = sorted(buckets)
        if not buckets:
            return {}
        rows = await self.db.query_raw(
            'SELECT bucket, block, block_hash FROM "tie_seed" '
            'WHERE bucket = ANY(string_to_array($1, \',\')::bigint[])',
            ",".join(str(b) for b in buckets))
        return {int(r["bucket"]): (int(r["block"]), r["block_hash"]) for r in rows}

    async def put_tie_seed(self, bucket, block, block_hash):
        await self.db.execute_raw(
            'INSERT INTO "tie_seed" (bucket, block, block_hash) VALUES ($1, $2, $3) '
            'ON CONFLICT (bucket) DO NOTHING', bucket, block, block_hash)

    async def bucket_claim_ids(self, buckets):
        buckets = sorted(buckets)
        if not buckets:
            return {}
        rows = await self.db.query_raw(
            'SELECT id, receipt_bucket AS bucket FROM "claim" '
            'WHERE receipt_bucket = ANY(string_to_array($1, \',\')::bigint[])',
            ",".join(str(b) for b in buckets))
        out: Dict[int, List[int]] = {}
        for r in rows:
            out.setdefault(int(r["bucket"]), []).append(int(r["id"]))
        return out

    async def take_jobs(self, kinds, limit, lease_ms, now_ms, skip_hotkeys=(), per_hotkey=0):
        if not kinds:
            return []
        rows = await self.db.query_raw(
            'UPDATE "job" SET status = \'running\', locked_at = now(), attempts = attempts + 1 '
            'WHERE id IN (SELECT j.id FROM "job" j JOIN (SELECT d.id, CASE WHEN c.hotkey IS NULL '
            'THEN 0 ELSE row_number() OVER (PARTITION BY c.hotkey '
            'ORDER BY c.receipt_bucket, d.id) END AS n, COALESCE(c.receipt_bucket, 0) AS b '
            'FROM "job" d LEFT JOIN "claim" c ON c.id = d.claim_id '
            'WHERE d.kind = ANY(string_to_array($1, \',\')) '
            "AND d.run_at <= to_timestamp($4::double precision / 1000) "
            "AND (d.status = 'pending' OR (d.status = 'running' "
            'AND d.locked_at < to_timestamp($2::double precision / 1000))) '
            'AND (c.hotkey IS NULL OR c.hotkey <> ALL(string_to_array($5, \',\')))) r '
            'ON r.id = j.id ORDER BY CASE WHEN $6::int > 0 THEN (r.n > $6::int)::int ELSE r.n END, '
            'r.b, j.id LIMIT $3 FOR UPDATE OF j SKIP LOCKED) '
            'RETURNING id, kind, claim_id, attempts',
            ",".join(kinds), now_ms - lease_ms, limit, now_ms, ",".join(skip_hotkeys), per_hotkey)
        return [Job(id=int(r["id"]), kind=r["kind"],
                    claim_id=int(r["claim_id"]) if r["claim_id"] is not None else None,
                    attempts=int(r["attempts"]), status="running") for r in rows]

    async def finish_job(self, job_id, error=None):
        await self.db.job.update(where={"id": job_id}, data={
            "status": "failed" if error else "done", "error": error, "lockedAt": None})

    async def defer_job(self, job_id, run_ms, retry=False):
        await self.db.job.update(where={"id": job_id}, data={
            "status": "pending", "runAt": _dt(run_ms), "lockedAt": None,
            **({} if retry else {"attempts": {"decrement": 1}})})

    async def add_job(self, kind, claim_id, run_ms):
        m = await self.db.job.create(data={"kind": kind, "claimId": claim_id,
                                           "runAt": _dt(run_ms)})
        return Job(id=m.id, kind=m.kind, claim_id=m.claimId, run_ms=run_ms)

    async def add_job_once(self, kind, claim_id, run_ms):
        rows = await self.db.query_raw(
            'INSERT INTO "job" (kind, claim_id, run_at) SELECT $1, $2::bigint, '
            'to_timestamp($3::double precision / 1000) WHERE NOT EXISTS (SELECT 1 FROM "job" '
            "WHERE kind = $1 AND claim_id = $2::bigint AND status IN ('pending', 'running')) "
            'RETURNING id', kind, claim_id, run_ms)
        if not rows:
            return None
        return Job(id=int(rows[0]["id"]), kind=kind, claim_id=claim_id, run_ms=run_ms)

    async def append_ledger(self, row):
        from prisma import Json

        m = await self.db.ledgerentry.create(data={
            "hotkey": row.hotkey, "claimId": row.claim_id, "eventId": row.event_id,
            "kind": row.kind, "points": row.points, "epoch": row.epoch, "route": row.route,
            "category": row.category, "reason": row.reason,
            "receivedMs": row.received_ms, "receiptBucket": row.bucket, "coldkey": row.coldkey,
            **({"factors": Json(row.factors)} if row.factors is not None else {}),
        })
        return _ledger_from(m)

    async def ledger_rows(self, epochs):
        rows = await self.db.ledgerentry.find_many(
            where={"epoch": {"in": list(epochs)}}, order={"id": "asc"})
        return [_ledger_from(m) for m in rows]

    async def claim_ledger(self, claim_id):
        rows = await self.db.ledgerentry.find_many(where={"claimId": claim_id},
                                                   order={"id": "asc"})
        return [_ledger_from(m) for m in rows]

    async def claims_for_event(self, event_id, statuses):
        rows = await self.db.claim.find_many(
            where={"eventId": event_id, "status": {"in": list(statuses)}}, order={"id": "asc"})
        return [_claim_from(m) for m in rows]

    async def claims_to_settle(self, now_ms, limit=200):
        rows = await self.db.claim.find_many(
            where={"status": "provisional", "held": False, "settleAt": {"lte": _dt(now_ms)}},
            order={"settleAt": "asc"}, take=limit)
        return [_claim_from(m) for m in rows]

    async def update_event(self, event_id, **fields):
        cols = {k: _EVENT_UPDATE[k] for k in fields if k in _EVENT_UPDATE}
        if not cols:
            return
        sets = ", ".join(f"{c} = " + _EVENT_CASTS.get(c, "${}").format(i + 2)
                         for i, c in enumerate(cols.values()))
        await self.db.execute_raw(f"UPDATE events SET {sets}, updated_at = now() WHERE id = $1",
                                  event_id, *(fields[k] for k in cols))

    async def add_facts(self, rows):
        from prisma import Json

        for r in rows:
            await self.db.eventfact.create(data={
                "eventId": r.event_id, "claimId": r.claim_id, "field": r.field,
                "value": Json({"value": r.value, "unit": r.unit}), "verified": True,
                "slot": r.slot, "docHash": r.doc_hash, "status": r.status, "paid": r.paid,
                "sourceUrl": r.source_url})

    async def event_facts(self, event_id):
        rows = await self.db.eventfact.find_many(where={"eventId": event_id}, order={"id": "asc"})
        out = []
        for m in rows:
            v = _json(m.value)
            out.append(EventFactRow(
                event_id=m.eventId, field=m.field, value=v.get("value"), unit=v.get("unit"),
                claim_id=m.claimId, slot=m.slot, doc_hash=m.docHash, status=m.status,
                paid=m.paid, source_url=m.sourceUrl, id=m.id))
        return out

    async def update_facts(self, claim_id, status, fields=None):
        where = {"claimId": claim_id, "status": "pending"}
        if fields is not None:
            where["field"] = {"in": list(fields)}
        await self.db.eventfact.update_many(where=where, data={"status": status})

    async def add_scoop_candidate(self, row):
        rows = await self.db.query_raw(
            'INSERT INTO "scoop_candidate" (scoop_id, url, validator, status, found_at) '
            "VALUES ($1, $2, $3, $4, to_timestamp($5::double precision / 1000)) "
            "ON CONFLICT (scoop_id, url) DO NOTHING RETURNING id",
            row.scoop_id, row.url, row.validator, row.status, row.found_ms)
        return replace(row, id=int(rows[0]["id"])) if rows else None

    async def scoop_candidates(self, scoop_id, statuses=None):
        rows = await self.db.query_raw(
            "SELECT id, scoop_id, url, validator, status, reason, "
            "(extract(epoch FROM found_at) * 1000)::bigint AS found_ms "
            'FROM "scoop_candidate" WHERE scoop_id = $1 ORDER BY id', scoop_id)
        return [ScoopCandidateRow(scoop_id=int(r["scoop_id"]), url=r["url"],
                                  validator=r["validator"], found_ms=int(r["found_ms"]),
                                  status=r["status"], reason=r["reason"], id=int(r["id"]))
                for r in rows if statuses is None or r["status"] in statuses]

    async def update_scoop_candidate(self, candidate_id, **fields):
        cols = {k: fields[k] for k in ("status", "reason") if k in fields}
        if not cols:
            return
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(cols))
        await self.db.execute_raw(f'UPDATE "scoop_candidate" SET {sets} WHERE id = $1',
                                  candidate_id, *cols.values())

    async def add_stage(self, event_id, stage, claim_id, at_ms):
        n = await self.db.execute_raw(
            "INSERT INTO event_stage (event_id, stage, claim_id, at) VALUES "
            "($1, $2, $3::bigint, to_timestamp($4::double precision / 1000)) "
            "ON CONFLICT DO NOTHING", event_id, stage, claim_id, at_ms)
        return n > 0

    async def get_attestation(self, signer, epoch):
        m = await self.db.attestation.find_unique(where={"uq_attestation_validator_epoch": {
            "validatorHotkey": signer, "epoch": int(epoch)}})
        if m is None or m.capacity is None:
            return None
        return {"perMinerPoints": _json(m.perMinerPoints), "totalPoints": m.totalPoints,
                "merkleRoot": m.merkleRoot, "signature": m.signature, "capacity": m.capacity,
                "leafCount": m.leafCount}

    async def attestation_issued_ms(self, signer, epoch):
        m = await self.db.attestation.find_unique(where={"uq_attestation_validator_epoch": {
            "validatorHotkey": signer, "epoch": int(epoch)}})
        return _ms(m.issuedAt) if m is not None and m.capacity is not None else None

    async def put_attestation(self, signer, epoch, body):
        from prisma import Json

        data = {"perMinerPoints": Json(body["perMinerPoints"]),
                "totalPoints": body["totalPoints"], "merkleRoot": body["merkleRoot"],
                "signature": body["signature"], "capacity": body["capacity"],
                "leafCount": body["leafCount"]}
        await self.db.attestation.upsert(
            where={"uq_attestation_validator_epoch": {"validatorHotkey": signer,
                                                      "epoch": int(epoch)}},
            data={"update": {}, "create": {"validatorHotkey": signer, "epoch": int(epoch),
                                           **data}})

    async def put_reverify(self, row):
        from prisma import Json

        data = {"epoch": row.epoch, "verdict": row.verdict, "observed": Json(row.observed)}
        await self.db.reverifyresult.upsert(
            where={"uq_reverify_claim_validator": {"claimId": row.claim_id,
                                                   "validatorHotkey": row.validator}},
            data={"update": data, "create": {"claimId": row.claim_id,
                                             "validatorHotkey": row.validator, **data}})

    async def add_reverify_pull(self, validator, epoch, at_ms):
        await self.db.reverifypull.create(data={"validatorHotkey": validator,
                                                "epoch": int(epoch), "at": _dt(at_ms)})

    async def reverify_claims(self, validator, epoch):
        rows = await self.db.reverifyresult.find_many(
            where={"validatorHotkey": validator, "epoch": int(epoch)})
        return {m.claimId for m in rows}

    async def add_reverify_report(self, validator, epoch, at_ms):
        n = await self.db.reverifypull.update_many(
            where={"validatorHotkey": validator, "epoch": int(epoch), "reportedAt": None},
            data={"reportedAt": _dt(at_ms)})
        if not n and not await self.db.reverifypull.find_first(
                where={"validatorHotkey": validator, "epoch": int(epoch)}):
            await self.db.reverifypull.create(data={
                "validatorHotkey": validator, "epoch": int(epoch), "at": _dt(at_ms),
                "reportedAt": _dt(at_ms)})

    async def reverify_reports(self, epochs):
        rows = await self.db.reverifypull.find_many(
            where={"epoch": {"in": list(epochs)}, "reportedAt": {"not": None}})
        out: Dict[Tuple[str, int], int] = {}
        for m in rows:
            key = (m.validatorHotkey, m.epoch)
            out[key] = min(out.get(key, _ms(m.reportedAt)), _ms(m.reportedAt))
        return out

    async def last_attestation_epoch(self, signer):
        m = await self.db.attestation.find_first(where={"validatorHotkey": signer},
                                                 order={"epoch": "desc"})
        return m.epoch if m is not None else None

    async def claim_points(self, claim_id):
        rows = await self.db.ledgerentry.group_by(
            by=["kind"], where={"claimId": claim_id}, sum={"points": True})
        return {r["kind"]: float(r["_sum"]["points"] or 0.0) for r in rows}

    async def get_event(self, event_id):
        rows = await self.db.query_raw(f"SELECT {_EVENT_COLS} FROM events WHERE id = $1", event_id)
        return _event_from(rows[0]) if rows else None

    async def event_by_identity(self, keys, fingerprint):
        rows = await self.db.query_raw(
            f"SELECT {_EVENT_COLS} FROM events WHERE fingerprint = $1 "
            "OR identity_key IN (SELECT jsonb_array_elements_text($2::jsonb)) "
            "ORDER BY id LIMIT 1", fingerprint, json.dumps(list(keys)))
        return _event_from(rows[0]) if rows else None

    async def event_candidates(self, date_lo, date_hi, actor_keys, event_type, event_date):
        rows = await self.db.query_raw(
            f"SELECT {_EVENT_COLS} FROM events WHERE event_date BETWEEN $1::date AND $2::date "
            "AND ((entities -> 'keys') ?| ARRAY(SELECT jsonb_array_elements_text($3::jsonb)) "
            "OR (event_type = $4 AND event_date BETWEEN $5::date - 1 AND $5::date + 1))",
            date_lo.isoformat(), date_hi.isoformat(), json.dumps(list(actor_keys)), event_type,
            event_date.isoformat())
        return [_event_from(r) for r in rows]

    async def add_event(self, rec):
        iso2, _, admin1 = (rec.place_key or "").partition("-")
        rows = await self.db.query_raw(
            "INSERT INTO events (fingerprint, event_type, canonical_title, event_date, category, "
            "identity_key, first_claim_id, stage, place_iso2, place_admin1, confidence, entities) "
            "VALUES ($1, $2, $3, $4::date, $5, $6, $7::bigint, $8, $9, $10, $11, $12::jsonb) "
            f"ON CONFLICT DO NOTHING RETURNING {_EVENT_COLS}",
            rec.fingerprint, rec.event_type, rec.headline or rec.event_type,
            rec.event_date.isoformat(), rec.category, rec.identity_key, rec.first_claim_id,
            rec.stage, iso2 or None, admin1 or None, rec.confidence,
            json.dumps({"keys": list(rec.actor_keys), "names": list(rec.names),
                        "subject": list(rec.subject_keys),
                        "numbers": [list(n) for n in rec.numbers]}))
        if not rows:
            raise DuplicateEvent(rec.fingerprint)
        return _event_from(rows[0])

    async def prize_docs(self, since_ms, probe=None):
        sql = ('SELECT doc_hash FROM "claim" WHERE doc_hash IS NOT NULL '
               "AND route = ANY(string_to_array($1, ',')) "
               "AND status = ANY(string_to_array($2, ',')) "
               'AND received_at >= to_timestamp($3::double precision / 1000)')
        args = [",".join(PRIZE_ROUTES), ",".join(WON), since_ms]
        if probe is not None:
            # Only rows that can match: the same URL hash, or at the same length the same lead
            # or one of the probe's minhash slots.
            url, lead, length, slots = probe
            sql += (" AND (left(doc_hash, 16) = $4::text OR (length(doc_hash) = $6::int AND ("
                    "($5::text <> '' AND substr(doc_hash, 17, 16) = $5::text) OR EXISTS ("
                    "SELECT 1 FROM unnest(string_to_array($7::text, ',')) WITH ORDINALITY "
                    "AS s(v, i) WHERE substr(doc_hash, 25 + 8 * i::int, 8) = s.v))))")
            args += [url, lead, length, ",".join(slots)]
        rows = await self.db.query_raw(sql, *args)
        return [r["doc_hash"] for r in rows]

    async def add_source_fetch(self, row):
        fields = {k: getattr(row, k) for k in (*_FETCH_FIELDS, "spans", "published_ms",
                                               "check")}
        m = await self.db.sourcefetch.create(data={"claimId": row.claim_id,
                                                   **_fetch_data(fields)})
        return _fetch_from(m)

    async def update_source_fetch(self, fetch_id, **fields):
        await self.db.sourcefetch.update(where={"id": fetch_id}, data=_fetch_data(fields))

    async def source_fetches(self, claim_id):
        rows = await self.db.sourcefetch.find_many(where={"claimId": claim_id},
                                                   order={"id": "asc"})
        return [_fetch_from(m) for m in rows]

    async def won_by_host(self, since_ms, hour_ms):
        rows = await self.db.query_raw(
            "SELECT lower(substring(source_url from '^[A-Za-z]+://([^/:?#]+)')) AS host, "
            "event_type, count(*) FILTER (WHERE verified_at >= to_timestamp($4::double "
            "precision / 1000)) AS hour, count(*) AS day FROM \"claim\" "
            "WHERE route = ANY(string_to_array($1, ',')) AND status = ANY(string_to_array($2, "
            "',')) AND verified_at >= to_timestamp($3::double precision / 1000) GROUP BY 1, 2",
            ",".join(PRIZE_ROUTES), ",".join(WON), since_ms, hour_ms)
        return [(r["host"] or "", r["event_type"], int(r["hour"]), int(r["day"])) for r in rows]

    async def fetches_since(self, since_ms, limit=100_000):
        rows = await self.db.sourcefetch.find_many(
            where={"fetchedAt": {"gte": _dt(since_ms)}}, order={"id": "desc"}, take=limit)
        return [_fetch_from(m) for m in reversed(rows)]

    async def held_claims(self, limit=500):
        rows = await self.db.claim.find_many(
            where={"held": True, "status": {"in": list(PASSED)}}, order={"id": "asc"},
            take=limit)
        return [_claim_from(m) for m in rows]

    _ITEM = ("SELECT id, key, claim_id, kind, hotkey, task, status, "
             "(extract(epoch FROM created_at) * 1000)::bigint AS created_ms FROM judge_item ")

    @staticmethod
    def _item(r) -> JudgeItemRow:
        task = r["task"]
        return JudgeItemRow(key=r["key"], claim_id=int(r["claim_id"]), kind=r["kind"],
                            task=json.loads(task) if isinstance(task, str) else dict(task),
                            created_ms=int(r["created_ms"]), hotkey=r["hotkey"],
                            status=r["status"], id=int(r["id"]))

    async def add_judge_item(self, row):
        await self.db.execute_raw(
            "INSERT INTO judge_item (key, claim_id, kind, hotkey, task, status, created_at) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, to_timestamp($7::double precision / 1000)) "
            "ON CONFLICT (key) DO NOTHING", row.key, row.claim_id, row.kind, row.hotkey,
            json.dumps(row.task), row.status, row.created_ms)
        return await self.judge_item(row.key)

    async def judge_item(self, key):
        rows = await self.db.query_raw(self._ITEM + "WHERE key = $1", key)
        return self._item(rows[0]) if rows else None

    async def judge_items(self, status="pending", limit=500):
        rows = await self.db.query_raw(self._ITEM + "WHERE status = $1 ORDER BY id LIMIT $2",
                                       status, limit)
        return [self._item(r) for r in rows]

    async def update_judge_item(self, item_id, status):
        await self.db.execute_raw("UPDATE judge_item SET status = $2 WHERE id = $1",
                                  item_id, status)

    async def judge_items_count(self, hotkey, since_ms):
        rows = await self.db.query_raw(
            "SELECT COUNT(*)::bigint AS n FROM judge_item WHERE hotkey = $1 "
            "AND created_at >= to_timestamp($2::double precision / 1000)", hotkey, since_ms)
        return int(rows[0]["n"]) if rows else 0

    async def add_judge_post(self, row):
        rows = await self.db.query_raw(
            "INSERT INTO judge_post (item_id, validator, body, signed_at, nonce, signature, "
            "posted_at) VALUES ($1, $2, $3, $4, $5, $6, "
            "to_timestamp($7::double precision / 1000)) "
            "ON CONFLICT (item_id, validator) DO NOTHING RETURNING id",
            row.item_id, row.validator, row.body, row.signed_at, row.nonce, row.signature,
            row.posted_ms)
        return replace(row, id=int(rows[0]["id"])) if rows else None

    async def judge_posts(self, item_ids):
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        rows = await self.db.query_raw(
            "SELECT id, item_id, validator, body, signed_at, nonce, signature, "
            "(extract(epoch FROM posted_at) * 1000)::bigint AS posted_ms FROM judge_post "
            "WHERE item_id = ANY($1::bigint[]) ORDER BY id", ids)
        return [JudgePostRow(item_id=int(r["item_id"]), validator=r["validator"],
                             body=r["body"], signed_at=int(r["signed_at"]), nonce=r["nonce"],
                             signature=r["signature"], posted_ms=int(r["posted_ms"]),
                             id=int(r["id"])) for r in rows]

    async def judge_verdict(self, key):
        m = await self.db.judgeverdict.find_unique(where={"key": key})
        return dict(m.record) if m else None

    async def add_judge_verdict(self, key, claim_id, record):
        from prisma import Json
        from prisma.errors import UniqueViolationError
        try:
            await self.db.judgeverdict.create(data={"key": key, "claimId": claim_id,
                                                    "record": Json(record)})
        except UniqueViolationError:
            pass

    async def claims_by_source(self, source_id, statuses):
        rows = await self.db.claim.find_many(
            where={"sourceId": source_id, "status": {"in": list(statuses)}}, order={"id": "asc"})
        return [_claim_from(m) for m in rows]

    async def sources(self, domain=None):
        rows = await self.db.source.find_many(
            where={"domain": domain} if domain is not None else {}, order={"id": "asc"})
        return [_source_from(m) for m in rows]

    async def get_source(self, source_id):
        m = await self.db.source.find_unique(where={"id": source_id})
        return _source_from(m) if m else None

    async def add_source(self, row):
        from prisma import Json

        key = {"uq_source_host_path": {"domain": row.domain, "host": row.host,
                                       "pathRule": row.path_rule}}
        m = await self.db.source.upsert(where=key, data={"update": {}, "create": {
            "domain": row.domain, "host": row.host, "pathRule": row.path_rule,
            "ownerId": row.owner_id,
            "status": row.status, "baseTrust": row.base_trust,
            "categoryTrust": Json(row.category_trust), "resetAt": _dt(row.reset_ms)}})
        return _source_from(m)

    async def update_source(self, source_id, **fields):
        from prisma import Json

        data = {_SOURCE_FIELDS[k]: v for k, v in fields.items() if k in _SOURCE_FIELDS}
        if "category_trust" in fields:
            data["categoryTrust"] = Json(fields["category_trust"])
        if "reset_ms" in fields:
            data["resetAt"] = _dt(fields["reset_ms"])
        await self.db.source.update(where={"id": source_id}, data=data)

    async def get_owner(self, owner_id):
        m = await self.db.owner.find_unique(where={"id": owner_id})
        return OwnerRow(m.id, _json(m.evidence), _ms(m.firstSeen)) if m else None

    async def put_owner(self, row):
        from prisma import Json

        data = {"evidence": Json(row.evidence)}
        if row.first_seen_ms:
            data["firstSeen"] = _dt(row.first_seen_ms)
        await self.db.owner.upsert(where={"id": row.id}, data={
            "update": data, "create": {"id": row.id, **data}})

    async def source_trust(self, source_id=None):
        rows = await self.db.sourcetrust.find_many(
            where={"sourceId": source_id} if source_id is not None else {},
            order=[{"sourceId": "asc"}, {"category": "asc"}])
        return [SourceTrustRow(m.sourceId, m.category, m.base, m.earned, m.penalties, m.trust,
                               m.judgedFirsts, m.confirmedFirsts, m.confirmOwners) for m in rows]

    async def put_source_trust(self, rows):
        for r in rows:
            data = {"base": r.base, "earned": r.earned, "penalties": r.penalties,
                    "trust": r.trust, "judgedFirsts": r.judged_firsts,
                    "confirmedFirsts": r.confirmed_firsts, "confirmOwners": r.confirm_owners}
            await self.db.sourcetrust.upsert(
                where={"uq_source_trust_source_category": {"sourceId": r.source_id,
                                                           "category": r.category}},
                data={"update": data,
                      "create": {"sourceId": r.source_id, "category": r.category, **data}})

    async def add_source_event(self, row):
        m = await self.db.sourceevent.create(data={
            "sourceId": row.source_id, "eventId": row.event_id, "claimId": row.claim_id,
            "ownerId": row.owner_id, "bySourceId": row.by_source_id, "role": row.role,
            "category": row.category, "publishedAt": _dt(row.published_ms),
            "at": _dt(row.at_ms)})
        return replace(row, id=m.id)

    async def source_events(self, source_id=None, event_id=None):
        where = {}
        if source_id is not None:
            where["sourceId"] = source_id
        if event_id is not None:
            where["eventId"] = event_id
        rows = await self.db.sourceevent.find_many(where=where, order={"id": "asc"})
        return [SourceEventRow(
            source_id=m.sourceId, role=m.role, category=m.category, at_ms=_ms(m.at),
            event_id=m.eventId, claim_id=m.claimId, owner_id=m.ownerId,
            by_source_id=m.bySourceId, published_ms=_ms(m.publishedAt), id=m.id) for m in rows]

    async def page_rules(self):
        return [PageRuleRow(m.pattern, m.kind, m.reason, m.createdBy, m.id)
                for m in await self.db.pagerule.find_many(order={"id": "asc"})]

    async def add_page_rule(self, row):
        data = {"kind": row.kind, "reason": row.reason, "createdBy": row.created_by}
        m = await self.db.pagerule.upsert(where={"pattern": row.pattern}, data={
            "update": data, "create": {"pattern": row.pattern, **data}})
        return PageRuleRow(m.pattern, m.kind, m.reason, m.createdBy, m.id)

    async def add_trust_log(self, row):
        await self.db.trustlog.create(data={
            "sourceId": row.source_id, "ruleId": row.rule_id, "field": row.field,
            "old": row.old, "new": row.new, "reason": row.reason, "actor": row.actor,
            **({"at": _dt(row.at_ms)} if row.at_ms else {})})

    async def trust_log(self, source_id=None, actor=None, since_ms=None):
        where = {}
        if source_id is not None:
            where["sourceId"] = source_id
        if actor is not None:
            where["actor"] = actor
        if since_ms is not None:
            where["at"] = {"gte": _dt(since_ms)}
        rows = await self.db.trustlog.find_many(where=where, order={"id": "asc"})
        return [TrustLogRow(m.field, m.old, m.new, m.reason, m.actor, m.sourceId, m.ruleId,
                            _ms(m.at), m.id) for m in rows]

    async def add_answer_items(self, rows):
        from prisma import Json
        from prisma.errors import UniqueViolationError

        added = []
        for r in rows:
            data = {"adapter": r.adapter, "externalId": r.external_id, "payload": Json(r.payload),
                    "category": r.category, "eventType": r.event_type, "status": r.status,
                    "eventDate": _day(r.event_date) if r.event_date else None,
                    "publishedAt": _dt(r.published_ms)}
            if r.observed_ms:
                data["observedAt"] = _dt(r.observed_ms)
            try:
                m = await self.db.answerkeyitem.create(
                    data={k: v for k, v in data.items() if v is not None})
            except UniqueViolationError:
                continue
            added.append(_answer_from(m))
        return added

    async def answer_items(self, statuses, observed_since_ms=None, limit=500):
        where = {"status": {"in": list(statuses)}}
        if observed_since_ms is not None:
            where["observedAt"] = {"gte": _dt(observed_since_ms)}
        rows = await self.db.answerkeyitem.find_many(where=where, order={"id": "asc"}, take=limit)
        return [_answer_from(m) for m in rows]

    async def update_answer_item(self, item_id, **fields):
        data = {_ANSWER_FIELDS[k]: v for k, v in fields.items() if k in _ANSWER_FIELDS}
        if "matched_ms" in fields:
            data["matchedAt"] = _dt(fields["matched_ms"])
        await self.db.answerkeyitem.update(where={"id": item_id}, data=data)

    async def add_accuracy_check(self, row):
        n = await self.db.execute_raw(
            "INSERT INTO accuracy_check (claim_id, hotkey, category, kind, ref, correct, at) "
            "VALUES ($1::bigint, $2, $3, $4, $5, $6, to_timestamp($7::double precision / 1000)) "
            "ON CONFLICT DO NOTHING", row.claim_id, row.hotkey, row.category, row.kind, row.ref,
            row.correct, row.at_ms)
        return n > 0

    async def accuracy_checks(self, since_ms, hotkeys=None, category=None):
        where = {"at": {"gte": _dt(since_ms)}}
        if hotkeys is not None:
            where["hotkey"] = {"in": list(hotkeys)}
        if category is not None:
            where["category"] = category
        rows = await self.db.accuracycheck.find_many(where=where, order={"id": "asc"})
        return [AccuracyCheckRow(m.claimId, m.hotkey, m.category, m.kind, m.correct, _ms(m.at),
                                 m.ref, m.id) for m in rows]

    async def put_miner_accuracy(self, rows):
        for r in rows:
            data = {"n": r.n, "correct": r.correct, "q": r.q,
                    "updatedAt": datetime.now(timezone.utc)}
            await self.db.mineraccuracy.upsert(
                where={"uq_miner_accuracy_hotkey_category": {"hotkey": r.hotkey,
                                                             "category": r.category}},
                data={"update": data,
                      "create": {"hotkey": r.hotkey, "category": r.category, **data}})

    async def miner_accuracy(self, hotkey=None):
        rows = await self.db.mineraccuracy.find_many(
            where={"hotkey": hotkey} if hotkey is not None else {},
            order=[{"hotkey": "asc"}, {"category": "asc"}])
        return [MinerAccuracyRow(m.hotkey, m.category, m.n, m.correct, m.q) for m in rows]

    async def claims_by_settle(self, since_ms, until_ms, statuses):
        rows = await self.db.claim.find_many(where={
            "status": {"in": list(statuses)},
            "settleAt": {"gte": _dt(since_ms), "lt": _dt(until_ms)}}, order={"id": "asc"})
        return [_claim_from(m) for m in rows]

    async def event_stages(self, event_id):
        rows = await self.db.eventstage.find_many(where={"eventId": event_id},
                                                  order=[{"at": "asc"}, {"id": "asc"}])
        return [(m.stage, m.claimId, _ms(m.at)) for m in rows]

    async def list_events(self, q):
        conds = ["publish_at IS NOT NULL", "publish_at <= to_timestamp($1::double precision / 1000)",
                 "hold IS NULL", "confidence = ANY(string_to_array($2, ','))"]
        args: list = [q.published_by_ms, ",".join(q.confidences)]

        def add(cond: str, value) -> None:
            args.append(value)
            conds.append(cond.format(f"${len(args)}"))

        for col, value in (("category", q.category), ("event_type", q.event_type),
                           ("stage", q.stage), ("storyline_id", q.storyline_id)):
            if value is not None:
                add(f"{col} = {{}}", value)
        if q.entity_keys:
            add("(entities -> 'keys') ?| ARRAY(SELECT jsonb_array_elements_text({}::jsonb))",
                json.dumps(list(q.entity_keys)))
        if q.since_ms is not None:
            add("first_seen_at >= to_timestamp({}::double precision / 1000)", q.since_ms)
        if q.until_ms is not None:
            add("first_seen_at < to_timestamp({}::double precision / 1000)", q.until_ms)
        if q.before is not None:
            args.append(q.before[0])
            add(f"(first_seen_at, id) < (to_timestamp(${len(args)}::double precision / 1000), "
                "{})", q.before[1])
        args.append(q.limit)
        rows = await self.db.query_raw(
            f"SELECT {_EVENT_COLS} FROM events WHERE {' AND '.join(conds)} "
            f"ORDER BY first_seen_at DESC, id DESC LIMIT ${len(args)}", *args)
        return [_event_from(r) for r in rows]

    async def add_feed(self, row):
        from prisma import Json

        m = await self.db.feedrecord.create(data={
            "at": _dt(row.at_ms), "kind": row.kind, "eventId": row.event_id,
            "category": row.category, "eventType": row.event_type, "payload": Json(row.payload)})
        return replace(row, seq=int(m.seq))

    async def feed(self, after_seq=0, until_ms=None, event_id=None, category=None,
                   event_type=None, entity_keys=(), limit=500):
        where: dict = {"seq": {"gt": after_seq}}
        if until_ms is not None:
            where["at"] = {"lte": _dt(until_ms)}
        for key, value in (("eventId", event_id), ("category", category),
                           ("eventType", event_type)):
            if value is not None:
                where[key] = value
        if entity_keys:
            rows = await self.db.query_raw(
                "SELECT id FROM events WHERE (entities -> 'keys') ?| "
                "ARRAY(SELECT jsonb_array_elements_text($1::jsonb))", json.dumps(list(entity_keys)))
            ids = [int(r["id"]) for r in rows]
            if event_id is not None and event_id not in ids:
                return []
            where.setdefault("eventId", {"in": ids})
        rows = await self.db.feedrecord.find_many(where=where, order={"seq": "asc"}, take=limit)
        return [FeedRow(kind=m.kind, event_id=m.eventId, category=m.category,
                        event_type=m.eventType, at_ms=_ms(m.at), payload=_json(m.payload),
                        seq=int(m.seq)) for m in rows]

    async def api_key_owner(self, key_hash):
        m = await self.db.apikey.find_unique(where={"keyHash": key_hash})
        return m.ownerId if m is not None and m.active else None

    async def add_api_key(self, key_hash, owner_id):
        await self.db.apikey.create(data={"keyHash": key_hash, "ownerId": owner_id})

    async def revoke_api_keys(self, owner_id=None, key_hash=None):
        where = {"active": True, **({"ownerId": owner_id} if owner_id else {}),
                 **({"keyHash": key_hash} if key_hash else {})}
        if len(where) == 1:
            return 0
        return await self.db.apikey.update_many(where=where, data={"active": False})

    async def registry_entities(self):
        return [record_from(m) for m in await self.db.entity.find_many(
            where={"origin": "registry"})]

    async def put_entity(self, record):
        # Linked rows are frozen on first use; unclear registry rows are refreshed.
        ext = canonical_id(record.scheme, record.external_id)
        where = {"uq_entity_scheme_external": {"scheme": record.scheme, "externalId": ext}}
        old = await self.db.entity.find_unique(where=where)
        if old is not None and not refreshes(record_from(old), record):
            return
        data = {"name": record.name, "kind": record.kind, "aliases": list(record.aliases),
                "xrefs": list(record.xrefs), "website": record.website,
                "status": record.status, "origin": record.origin, "revision": record.revision,
                "checkedAt": _dt(record.checked_ms)}
        await self.db.entity.upsert(where=where, data={
            "create": {"scheme": record.scheme, "externalId": ext, **data}, "update": data})

    async def miner_stats(self, since_ms, until_ms, category=None):
        where = ("c.received_at >= to_timestamp($1::double precision / 1000) "
                 "AND c.received_at < to_timestamp($2::double precision / 1000)")
        args: list = [since_ms, until_ms]
        if category is not None:
            where += " AND c.category = $3"
            args.append(category)
        out: Dict[str, MinerStats] = {}
        for r in await self.db.query_raw(
                "SELECT c.hotkey, c.reason, c.status IN ('rejected', 'clawed_back') AS rejected, "
                "count(*) AS n, count(*) FILTER (WHERE c.verified_at IS NOT NULL) AS verified "
                f"FROM claim c WHERE {where} "
                "GROUP BY 1, 2, 3", *args):
            s = out.setdefault(r["hotkey"], MinerStats())
            s.claims += int(r["n"])
            s.verified += int(r["verified"])
            if r["rejected"] and r["reason"]:
                s.rejected[r["reason"]] = s.rejected.get(r["reason"], 0) + int(r["n"])
        for r in await self.db.query_raw(
                "SELECT c.hotkey, l.route, l.kind, sum(l.points)::float8 AS pts "
                f"FROM ledger_entry l JOIN claim c ON c.id = l.claim_id WHERE {where} "
                "GROUP BY 1, 2, 3", *args):
            s = out.setdefault(r["hotkey"], MinerStats())
            s.points.setdefault(r["route"] or "", {})[r["kind"]] = float(r["pts"])
        return out

    async def claims_page(self, until_ms, hotkey=None, route=None, status=None, category=None,
                          since_ms=None, before_id=None, limit=50, event_type=None, reason=None):
        cutoff = _dt(until_ms)
        where: dict = {"receivedAt": {"lte": cutoff},
                       "OR": [{"judgedAt": None}, {"judgedAt": {"lte": cutoff}}]}
        for key, value in (("hotkey", hotkey), ("route", route), ("status", status),
                           ("category", category), ("eventType", event_type),
                           ("reason", reason)):
            if value is not None:
                where[key] = value
        if since_ms is not None:
            where["receivedAt"]["gte"] = _dt(since_ms)
        if before_id is not None:
            where["id"] = {"lt": before_id}
        rows = await self.db.claim.find_many(where=where, order={"id": "desc"}, take=limit)
        return [_claim_from(m) for m in rows]

    async def banded_claims(self, since_ms):
        rows = await self.db.claim.find_many(
            where={"trustBand": {"not": None}, "verifiedAt": {"gte": _dt(since_ms)}},
            order={"id": "asc"})
        return [_claim_from(m) for m in rows]

    async def claims_of_types(self, since_ms, event_types):
        rows = await self.db.claim.find_many(
            where={"eventType": {"in": list(event_types)}, "receivedAt": {"gte": _dt(since_ms)}},
            order={"id": "asc"})
        return [_claim_from(m) for m in rows]

    async def ledger_for_claims(self, claim_ids):
        rows = await self.db.ledgerentry.find_many(
            where={"claimId": {"in": list(claim_ids)}}, order={"id": "asc"})
        return [_ledger_from(m) for m in rows]

    async def reverify_for_claims(self, claim_ids):
        rows = await self.db.reverifyresult.find_many(
            where={"claimId": {"in": list(claim_ids)}}, order={"id": "asc"})
        return [ReverifyRow(m.claimId, m.validatorHotkey, m.epoch, m.verdict,
                            _json(m.observed), m.id) for m in rows]

    async def fetches_for_claims(self, claim_ids):
        rows = await self.db.sourcefetch.find_many(
            where={"claimId": {"in": list(claim_ids)}}, order={"id": "asc"})
        return [_fetch_from(m) for m in rows]

    async def claims_by_ids(self, claim_ids):
        rows = await self.db.claim.find_many(where={"id": {"in": sorted(set(claim_ids))}},
                                             order={"id": "asc"})
        return [_claim_from(m) for m in rows]

    async def judge_items_for_claims(self, claim_ids):
        ids = sorted({int(i) for i in claim_ids})
        if not ids:
            return []
        rows = await self.db.query_raw(
            self._ITEM + "WHERE claim_id = ANY($1::bigint[]) ORDER BY id", ids)
        return [self._item(r) for r in rows]

    async def judge_posts_since(self, since_ms):
        rows = await self.db.query_raw(
            "SELECT p.id, p.item_id, i.claim_id, p.validator, p.body, p.signed_at, p.nonce, "
            "p.signature, (extract(epoch FROM p.posted_at) * 1000)::bigint AS posted_ms "
            "FROM judge_post p JOIN judge_item i ON i.id = p.item_id "
            "WHERE p.posted_at >= to_timestamp($1::double precision / 1000) ORDER BY p.id",
            since_ms)
        return [(int(r["claim_id"]), JudgePostRow(
            item_id=int(r["item_id"]), validator=r["validator"], body=r["body"],
            signed_at=int(r["signed_at"]), nonce=r["nonce"], signature=r["signature"],
            posted_ms=int(r["posted_ms"]), id=int(r["id"]))) for r in rows]

    async def candidates_for_scoops(self, scoop_ids):
        ids = sorted({int(i) for i in scoop_ids})
        if not ids:
            return []
        rows = await self.db.query_raw(
            "SELECT id, scoop_id, url, validator, status, reason, "
            "(extract(epoch FROM found_at) * 1000)::bigint AS found_ms "
            'FROM "scoop_candidate" WHERE scoop_id = ANY($1::bigint[]) ORDER BY id', ids)
        return [ScoopCandidateRow(scoop_id=int(r["scoop_id"]), url=r["url"],
                                  validator=r["validator"], found_ms=int(r["found_ms"]),
                                  status=r["status"], reason=r["reason"], id=int(r["id"]))
                for r in rows]

    async def reverify_reports_since(self, since_ms):
        rows = await self.db.reverifypull.find_many(
            where={"reportedAt": {"gte": _dt(since_ms)}})
        out: Dict[Tuple[str, int], int] = {}
        for m in rows:
            key = (m.validatorHotkey, m.epoch)
            out[key] = min(out.get(key, _ms(m.reportedAt)), _ms(m.reportedAt))
        return out

    async def reverify_in_epochs(self, epochs):
        rows = await self.db.reverifyresult.find_many(
            where={"epoch": {"in": sorted({int(e) for e in epochs})}}, order={"id": "asc"})
        return [ReverifyRow(m.claimId, m.validatorHotkey, m.epoch, m.verdict,
                            _json(m.observed), m.id) for m in rows]


_EVENT_UPDATE = {"confidence": "confidence", "first_claim_id": "first_claim_id",
                 "stage": "stage", "first_seen_ms": "first_seen_at",
                 "official_match": "official_match", "publish_ms": "publish_at",
                 "hold": "hold", "storyline_id": "storyline_id", "marked": "marked"}
_EVENT_CASTS = {"first_claim_id": "${}::bigint",
                "first_seen_at": "to_timestamp(${}::double precision / 1000)",
                "publish_at": "to_timestamp(${}::double precision / 1000)"}

_SOURCE_FIELDS = {"owner_id": "ownerId", "status": "status", "base_trust": "baseTrust"}

_ANSWER_FIELDS = {"status": "status", "matched_claim_id": "matchedClaimId",
                  "matched_event_id": "matchedEventId"}


def _answer_from(m) -> AnswerKeyRow:
    return AnswerKeyRow(
        adapter=m.adapter, external_id=m.externalId, payload=_json(m.payload),
        category=m.category, event_type=m.eventType,
        event_date=m.eventDate.date() if m.eventDate else None,
        published_ms=_ms(m.publishedAt), status=m.status, matched_claim_id=m.matchedClaimId,
        matched_event_id=m.matchedEventId, matched_ms=_ms(m.matchedAt),
        observed_ms=_ms(m.observedAt), id=m.id)


def _json(v) -> dict:
    v = json.loads(v) if isinstance(v, str) else v
    return v if isinstance(v, dict) else {}


def _source_from(m) -> SourceRow:
    return SourceRow(domain=m.domain, path_rule=m.pathRule, owner_id=m.ownerId, status=m.status,
                     base_trust=m.baseTrust, category_trust=_json(m.categoryTrust),
                     reset_ms=_ms(m.resetAt), id=m.id, host=m.host)


def _day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


_EVENT_COLS = (
    "id, event_type, category, event_date::text AS event_date, fingerprint, canonical_title, "
    "entities, place_iso2, place_admin1, identity_key, stage, first_claim_id, confidence, "
    "official_match, (extract(epoch FROM first_seen_at) * 1000)::bigint AS first_seen_ms, "
    "(extract(epoch FROM publish_at) * 1000)::bigint AS publish_ms, hold, storyline_id, marked")


def _event_from(r: dict) -> EventRecord:
    ent = r.get("entities")
    ent = json.loads(ent) if isinstance(ent, str) else ent
    ent = ent if isinstance(ent, dict) else {}
    iso2, admin1 = r.get("place_iso2"), r.get("place_admin1")
    return EventRecord(
        id=int(r["id"]), event_type=r["event_type"], category=r["category"],
        event_date=date.fromisoformat(str(r["event_date"])[:10]), fingerprint=r["fingerprint"],
        headline=r["canonical_title"], actor_keys=tuple(ent.get("keys", ())),
        names=tuple(ent.get("names", ())), subject_keys=tuple(ent.get("subject", ())),
        numbers=tuple(tuple(n) for n in ent.get("numbers", ())),
        place_key=f"{iso2}-{admin1}" if iso2 and admin1 else (iso2 or ""),
        identity_key=r["identity_key"], stage=r["stage"],
        first_claim_id=int(r["first_claim_id"]) if r["first_claim_id"] is not None else None,
        confidence=r["confidence"], official_match=bool(r.get("official_match")),
        first_seen_ms=int(r["first_seen_ms"]) if r["first_seen_ms"] is not None else None,
        publish_ms=int(r["publish_ms"]) if r.get("publish_ms") is not None else None,
        hold=r.get("hold"), storyline_id=r.get("storyline_id"), marked=r.get("marked"))
