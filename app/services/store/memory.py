"""In-memory store for tests."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import itertools
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields, replace
from typing import Dict, List, Optional, Tuple

from app.services.entities import EntityRecord, refreshes
from app.services.store.rows import (
    BEHIND, CHECKED, DECIDED, INTAKE_REJECTS, PASSED, PRIZE_ROUTES, QUALITY_PAID, SCOOP_MISSES, WON, AccuracyCheckRow, AnswerKeyRow,
    ClaimHead, ClaimRow, DuplicateClaim, DuplicateEvent, EventFactRow, EventQuery, EventRecord, FeedRow,
    HotkeyStats, Job, JudgeItemRow, JudgePostRow, LedgerRow, MinerAccuracyRow, MinerStats, OwnerRow, PageRuleRow,
    ReverifyRow, ScoopCandidateRow, SourceEventRow, SourceFetchRow, SourceRow, SourceTrustRow, TrustLogRow,
)


def _event_matches(e: EventRecord, q: EventQuery) -> bool:
    first = e.first_seen_ms or 0
    return (e.publish_ms is not None and e.publish_ms <= q.published_by_ms and e.hold is None
            and e.confidence in q.confidences
            and q.category in (None, e.category) and q.event_type in (None, e.event_type)
            and q.stage in (None, e.stage) and q.storyline_id in (None, e.storyline_id)
            and (not q.entity_keys or bool(set(q.entity_keys) & set(e.actor_keys)))
            and (q.since_ms is None or first >= q.since_ms)
            and (q.until_ms is None or first < q.until_ms)
            and (q.before is None or (first, e.id) < q.before))


def _visible(c: ClaimRow, until_ms: int) -> bool:
    return c.received_ms <= until_ms and (c.judged_ms is None or c.judged_ms <= until_ms)


def _counts(claim: ClaimRow, ahead_of: ClaimRow, since_ms: int) -> bool:
    return (claim.hotkey == ahead_of.hotkey and claim.received_ms >= since_ms
            and (claim.received_ms, claim.id) < (ahead_of.received_ms, ahead_of.id)
            and claim.reason not in INTAKE_REJECTS)


SHARED, EXCLUSIVE = "shared", "exclusive"
# The memory transaction the running task is in.
_tx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("memory_tx", default=None)


class _SharedLock:
    """Postgres' advisory lock as the ledger uses it: many shared holders or one exclusive,
    and a waiting exclusive one goes before shared ones that come after it."""

    def __init__(self):
        self.readers, self.writer, self.waiting = 0, False, 0
        self._cond: Optional[asyncio.Condition] = None
        self._loop = None

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond, self._loop = asyncio.Condition(), loop
        return self._cond

    async def acquire(self, exclusive: bool) -> None:
        cond = self._condition()
        async with cond:
            if exclusive:
                self.waiting += 1
                try:
                    await cond.wait_for(lambda: not self.writer and not self.readers)
                finally:
                    self.waiting -= 1
                self.writer = True
            else:
                await cond.wait_for(lambda: not self.writer and not self.waiting)
                self.readers += 1

    async def release(self, exclusive: bool) -> None:
        cond = self._condition()
        async with cond:
            if exclusive:
                self.writer = False
            else:
                self.readers -= 1
            cond.notify_all()


@dataclass
class MemoryStore:
    claims: Dict[int, ClaimRow] = field(default_factory=dict)
    jobs: Dict[int, Job] = field(default_factory=dict)
    ledger: List[LedgerRow] = field(default_factory=list)
    blacklist: set = field(default_factory=set)
    events: Dict[int, EventRecord] = field(default_factory=dict)
    event_added_ms: Dict[int, int] = field(default_factory=dict)
    fetches: List[SourceFetchRow] = field(default_factory=list)
    judge_item_rows: Dict[int, JudgeItemRow] = field(default_factory=dict)
    judge_post_rows: List[JudgePostRow] = field(default_factory=list)
    verdicts: Dict[str, Tuple[Optional[int], dict]] = field(default_factory=dict)
    source_rows: Dict[int, SourceRow] = field(default_factory=dict)
    owner_rows: Dict[str, OwnerRow] = field(default_factory=dict)
    trust_rows: Dict[Tuple[int, str], SourceTrustRow] = field(default_factory=dict)
    source_event_rows: List[SourceEventRow] = field(default_factory=list)
    rule_rows: Dict[str, PageRuleRow] = field(default_factory=dict)
    log_rows: List[TrustLogRow] = field(default_factory=list)
    fact_rows: List[EventFactRow] = field(default_factory=list)
    stage_rows: Dict[Tuple[int, str], Tuple[Optional[int], int]] = field(default_factory=dict)
    attestations: Dict[Tuple[str, int], dict] = field(default_factory=dict)
    attestation_ms: Dict[Tuple[str, int], int] = field(default_factory=dict)
    reverify: Dict[Tuple[int, str], ReverifyRow] = field(default_factory=dict)
    pulls: List[Tuple[str, int, int]] = field(default_factory=list)
    reports: Dict[Tuple[str, int], int] = field(default_factory=dict)
    answer_rows: Dict[Tuple[str, str], AnswerKeyRow] = field(default_factory=dict)
    check_rows: Dict[Tuple[int, str, str], AccuracyCheckRow] = field(default_factory=dict)
    accuracy_rows: Dict[Tuple[str, str], MinerAccuracyRow] = field(default_factory=dict)
    feed_rows: List[FeedRow] = field(default_factory=list)
    api_keys: Dict[str, str] = field(default_factory=dict)
    tie_rows: Dict[int, Tuple[int, str]] = field(default_factory=dict)
    entity_rows: Dict[str, EntityRecord] = field(default_factory=dict)
    candidate_rows: Dict[int, ScoopCandidateRow] = field(default_factory=dict)
    receipt_rows: List[dict] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _locked: Dict[int, int] = field(default_factory=dict)
    # Claim id -> when it last changed, in ms.
    changed: Dict[int, int] = field(default_factory=dict)

    _ledger_lock: "_SharedLock" = field(default_factory=lambda: _SharedLock(), repr=False,
                                        compare=False)
    _claim_locks: Dict[int, asyncio.Lock] = field(default_factory=dict, repr=False,
                                                  compare=False)

    @asynccontextmanager
    async def transaction(self, timeout_ms: Optional[int] = None):
        """Puts every table back as it was if the block raises, and holds the ledger lock
        taken inside it until it ends. Rows are replaced rather than changed in place, so a
        copy of each table is enough. Writes by other tasks in the meantime are undone too
        (from when the ledger lock was taken, when it was); tests do not rely on them. A
        transaction inside another is the outer one."""
        frame = _tx.get()
        if frame is not None and frame["store"] is self:
            yield self
            return
        frame = {"store": self, "held": None, "claims": [], "saved": self._tables()}
        token = _tx.set(frame)
        try:
            yield self
        except BaseException:
            for name, table in frame["saved"].items():
                setattr(self, name, table)
            raise
        finally:
            _tx.reset(token)
            for claim_id in frame["claims"]:
                self._claim_locks[claim_id].release()
            if frame["held"] is not None:
                await self._ledger_lock.release(frame["held"] == EXCLUSIVE)

    def _tables(self) -> dict:
        return {f.name: copy.copy(getattr(self, f.name)) for f in fields(self)
                if f.name not in ("_ids", "_ledger_lock", "_claim_locks")}

    async def lock_ledger(self, exclusive: bool = False) -> Optional[int]:
        """The ledger lock, shared or exclusive, held until the transaction ends. Taken first
        in its transaction; a wait past app.db.LEDGER_LOCK_TIMEOUT_MS fails. No store clock:
        None."""
        frame = _tx.get()
        if frame is None or frame["store"] is not self:
            raise RuntimeError("the ledger lock is taken inside a transaction")
        want = EXCLUSIVE if exclusive else SHARED
        if frame["held"] in (want, EXCLUSIVE):
            return None
        if frame["held"] is not None:
            raise RuntimeError("a shared ledger lock is not raised to exclusive")
        from app import db
        try:
            await asyncio.wait_for(self._ledger_lock.acquire(exclusive),
                                   db.LEDGER_LOCK_TIMEOUT_MS / 1000)
        except asyncio.TimeoutError:
            raise TimeoutError("ledger lock not available") from None
        frame["held"] = want
        # What other tasks wrote while this one waited is theirs to keep.
        frame["saved"] = self._tables()
        return None

    async def lock_claim(self, claim_id):
        frame = _tx.get()
        if frame is None or frame["store"] is not self:
            raise RuntimeError("a claim is locked inside a transaction")
        if claim_id in frame["claims"]:
            return
        await self._claim_locks.setdefault(claim_id, asyncio.Lock()).acquire()
        frame["claims"].append(claim_id)

    def ledger_lock_held(self) -> Optional[str]:
        """How this task's transaction holds the ledger lock, if it does."""
        frame = _tx.get()
        return frame["held"] if frame is not None and frame["store"] is self else None

    async def is_blacklisted(self, hotkey):
        return hotkey in self.blacklist

    async def set_blacklisted(self, hotkey, blocked, reason=None):
        (self.blacklist.add if blocked else self.blacklist.discard)(hotkey)

    async def claim_by_body(self, hotkey, body_hash):
        return next((c for c in self.claims.values()
                     if c.hotkey == hotkey and c.body_hash == body_hash), None)

    async def create_claim(self, row, jobs=(), receipt=None):
        if any(c.hotkey == row.hotkey and (c.nonce == row.nonce or c.body_hash == row.body_hash)
               for c in self.claims.values()):
            raise DuplicateClaim(row.hotkey)
        row = replace(row, id=next(self._ids))
        self.claims[row.id] = row
        self._touch(row.id)
        if receipt is not None:
            self.receipt_rows.append(
                dict(receipt(row.id, self.receipt_rows[-1] if self.receipt_rows else None)))
        for kind in jobs:
            job_id = next(self._ids)
            self.jobs[job_id] = Job(id=job_id, kind=kind, claim_id=row.id)
        return replace(row)

    async def receipt_of(self, claim_id):
        return next((dict(r) for r in self.receipt_rows if r["claim_id"] == str(claim_id)),
                    None)

    async def receipts_for(self, claim_ids):
        want = {str(i) for i in claim_ids}
        return {int(r["claim_id"]): dict(r) for r in self.receipt_rows if r["claim_id"] in want}

    async def get_claim(self, claim_id):
        c = self.claims.get(claim_id)
        return replace(c) if c else None

    async def update_claim(self, claim_id, expect, **fields):
        c = self.claims.get(claim_id)
        if c is None or c.status != expect:
            return False
        if "status" in fields and fields["status"] not in DECIDED:
            fields = {**fields, "decided_ms": None}
        self.claims[claim_id] = replace(c, **fields)
        self._touch(claim_id)
        return True

    async def claims_by_status(self, status, received_before_ms=None, limit=500, skip=(),
                               after=None):
        skip = set(skip)
        rows = [c for c in self.claims.values() if c.status == status and c.id not in skip
                and (received_before_ms is None or c.received_ms < received_before_ms)
                and (after is None or (c.bucket, c.id) > tuple(after))]
        return [replace(c) for c in sorted(rows, key=lambda c: (c.bucket, c.id))[:limit]]

    async def judge_repeats(self, owner, event_type, doc_hash, since_ms, judged_since_ms,
                            exclude_id, reasons):
        return sum(1 for c in await self.claims_of_types(since_ms, [event_type])
                   if c.id != exclude_id and (c.coldkey or c.hotkey) == owner
                   and c.doc_hash == doc_hash and (c.judged_ms or 0) >= judged_since_ms
                   and c.reason in reasons)

    def _touch(self, claim_id):
        import time
        self.changed[claim_id] = max(time.time_ns() // 1_000_000,
                                     max(self.changed.values(), default=0))

    async def claim_changes(self, hotkey, after, until_ms, limit):
        rows = sorted((ms, cid) for cid, ms in self.changed.items()
                      if self.claims[cid].hotkey == hotkey and (ms, cid) > tuple(after)
                      and ms < until_ms)
        return [(cid, ms) for ms, cid in rows[:limit]]

    async def claim_heads(self, status, limit=500, skip=()):
        return [ClaimHead(c.id, c.event_id, c.source_url, c.received_ms,
                          (c.payload or {}).get("event_date"))
                for c in await self.claims_by_status(status, limit=limit, skip=skip)]

    async def claims_ahead(self, claim, since_ms):
        return sum(_counts(c, claim, since_ms) for c in self.claims.values())

    def _window(self, since_ms, hotkey=None):
        return sorted((c for c in self.claims.values()
                       if c.received_ms >= since_ms and c.reason not in INTAKE_REJECTS
                       and (hotkey is None or c.hotkey == hotkey)),
                      key=lambda c: (c.received_ms, c.id))

    async def budget_use(self, hotkey, since_ms):
        rows = self._window(since_ms, hotkey)
        return len(rows), rows[0].received_ms if rows else None

    async def queued_hotkeys(self):
        return sorted({c.hotkey for c in self.claims.values() if c.status == "queued"})

    async def admissible_claims(self, limits, since_ms, limit):
        seen: Dict[str, int] = {}
        out = []
        for c in self._window(since_ms):
            seen[c.hotkey] = n = seen.get(c.hotkey, 0) + 1
            if c.status == "queued" and n <= limits.get(c.hotkey, 0):
                out.append(c)
        return [replace(c) for c in sorted(out, key=lambda c: (c.bucket, c.id))[:limit]]

    async def quality_outcomes(self, since_ms, junk, until_ms=None):
        none = {f.claim_id for f in self.fetches
                if ((f.check or {}).get("judge") or {}).get("type_none")}
        rows = [c for c in self.claims.values() if c.decided_ms is not None
                and c.decided_ms >= since_ms and (until_ms is None or c.decided_ms < until_ms)
                and (c.status in QUALITY_PAID or (c.status == "rejected"
                                                   and c.judged_ms is not None and (
                    c.reason in junk or (c.reason == "judge_unclear" and c.id in none))))]
        return [(c.coldkey or c.hotkey, c.status in QUALITY_PAID, c.id)
                for c in sorted(rows, key=lambda c: (c.decided_ms, c.id))]

    async def hotkey_stats(self, since_ms, hotkeys=None):
        wanted = set(hotkeys) if hotkeys is not None else None
        out: Dict[str, HotkeyStats] = {}
        for c in self.claims.values():
            if c.judged_ms is None or c.judged_ms < since_ms:
                continue
            if wanted is not None and c.hotkey not in wanted:
                continue
            s = out.setdefault(c.hotkey, HotkeyStats())
            s.passes += c.status in PASSED
        return out

    async def scoop_record(self, since_ms, owners):
        wanted = set(owners)
        out: Dict[str, Tuple[int, int]] = {}
        for c in self.claims.values():
            owner = c.coldkey or c.hotkey
            if c.route != "scoop" or owner not in wanted or c.received_ms < since_ms:
                continue
            hit = c.wrong is None and c.status in PASSED
            if hit or c.wrong is not None or c.status == "clawed_back" or c.reason in SCOOP_MISSES:
                hits, judged = out.get(owner, (0, 0))
                out[owner] = (hits + hit, judged + 1)
        return out

    async def open_scoops(self, owners):
        wanted, out = set(owners), {}
        for c in self.claims.values():
            owner = c.coldkey or c.hotkey
            if c.route == "scoop" and c.status == "verified" and owner in wanted:
                out[owner] = out.get(owner, 0) + 1
        return out

    async def first_trusted_ms(self, event_id, exclude_id, trust_min):
        ids = {c.id for c in self.claims.values() if c.event_id == event_id
               and c.id != exclude_id and (c.trust or 0.0) >= trust_min
               and (c.status in CHECKED or c.reason in BEHIND)}
        times = [self.claims[i].received_ms for i in ids]
        times += [f.published_ms for f in self.fetches
                  if f.claim_id in ids and f.published_ms is not None]
        return min(times, default=None)

    async def tie_seeds(self, buckets):
        return {b: self.tie_rows[b] for b in buckets if b in self.tie_rows}

    async def put_tie_seed(self, bucket, block, block_hash):
        self.tie_rows.setdefault(bucket, (block, block_hash))

    async def bucket_claim_ids(self, buckets):
        wanted = set(buckets)
        out: Dict[int, List[int]] = {}
        for c in self.claims.values():
            if c.bucket in wanted:
                out.setdefault(c.bucket, []).append(c.id)
        return out

    async def take_jobs(self, kinds, limit, lease_ms, now_ms, skip_hotkeys=(), per_hotkey=0):
        skip, rank, due = set(skip_hotkeys), {}, []

        def bucket(j):
            c = self.claims.get(j.claim_id)
            return c.bucket if c else 0

        for job in sorted(self.jobs.values(), key=lambda j: (bucket(j), j.id)):
            stale = job.status == "running" and self._locked.get(job.id, 0) < now_ms - lease_ms
            claim = self.claims.get(job.claim_id)
            hotkey = claim.hotkey if claim else None
            if (job.kind in kinds and job.run_ms <= now_ms and (job.status == "pending" or stale)
                    and hotkey not in skip):
                rank[hotkey] = n = rank.get(hotkey, 0) + 1
                n = n if hotkey else 0
                due.append((int(n > per_hotkey) if per_hotkey else n, bucket(job), job.id, job))
        taken = []
        for *_, job in sorted(due, key=lambda d: d[:3])[:limit]:
            job.status, job.attempts = "running", job.attempts + 1
            self._locked[job.id] = now_ms
            taken.append(replace(job))
        return taken

    async def finish_job(self, job_id, error=None):
        job = self.jobs[job_id]
        job.status, job.error = ("failed", error) if error else ("done", None)

    async def defer_job(self, job_id, run_ms, retry=False):
        job = self.jobs[job_id]
        job.status, job.run_ms = "pending", run_ms
        if not retry:
            job.attempts = max(0, job.attempts - 1)
        self._locked.pop(job_id, None)

    async def add_job(self, kind, claim_id, run_ms):
        job = Job(id=next(self._ids), kind=kind, claim_id=claim_id, run_ms=run_ms)
        self.jobs[job.id] = job
        return replace(job)

    async def add_job_once(self, kind, claim_id, run_ms):
        if any(j.kind == kind and j.claim_id == claim_id and j.status in ("pending", "running")
               for j in self.jobs.values()):
            return None
        return await self.add_job(kind, claim_id, run_ms)

    async def append_ledger(self, row):
        row = replace(row, id=next(self._ids))
        self.ledger.append(row)
        return replace(row)

    async def ledger_rows(self, epochs, after_id=0, limit=None):
        wanted = set(epochs)
        rows = [r for r in self.ledger if r.epoch in wanted
                and (not after_id or (r.id or 0) > after_id)]
        return [replace(r) for r in rows[:limit]]

    async def claim_ledger(self, claim_id):
        return [replace(r) for r in self.ledger if r.claim_id == claim_id]

    async def claim_points(self, claim_id):
        out: Dict[str, float] = {}
        for r in self.ledger:
            if r.claim_id == claim_id:
                out[r.kind] = out.get(r.kind, 0.0) + r.points
        return out

    async def claims_for_event(self, event_id, statuses):
        rows = [c for c in self.claims.values()
                if c.event_id == event_id and c.status in statuses]
        return [replace(c) for c in sorted(rows, key=lambda c: c.id)]

    async def claims_to_settle(self, now_ms, limit=200):
        rows = [c for c in self.claims.values() if c.status == "provisional" and not c.held
                and c.settle_ms is not None and c.settle_ms <= now_ms]
        return [replace(c) for c in sorted(rows, key=lambda c: c.settle_ms)[:limit]]

    async def update_event(self, event_id, **fields):
        self.events[event_id] = replace(self.events[event_id], **fields)

    async def add_facts(self, rows):
        for r in rows:
            r = replace(r, id=next(self._ids))
            self.fact_rows.append(r)

    async def event_facts(self, event_id):
        return [replace(r) for r in self.fact_rows if r.event_id == event_id]

    async def update_facts(self, claim_id, status, fields=None):
        self.fact_rows = [replace(r, status=status)
                          if r.claim_id == claim_id and r.status == "pending"
                          and (fields is None or r.field in fields) else r
                          for r in self.fact_rows]

    async def add_scoop_candidate(self, row):
        if any(c.scoop_id == row.scoop_id and c.url == row.url
               for c in self.candidate_rows.values()):
            return None
        row = replace(row, id=next(self._ids))
        self.candidate_rows[row.id] = row
        return replace(row)

    async def scoop_candidates(self, scoop_id, statuses=None):
        return [replace(c) for c in sorted(self.candidate_rows.values(), key=lambda c: c.id)
                if c.scoop_id == scoop_id and (statuses is None or c.status in statuses)]

    async def update_scoop_candidate(self, candidate_id, **fields):
        self.candidate_rows[candidate_id] = replace(self.candidate_rows[candidate_id], **fields)

    async def add_stage(self, event_id, stage, claim_id, at_ms):
        if (event_id, stage) in self.stage_rows:
            return False
        self.stage_rows[(event_id, stage)] = (claim_id, at_ms)
        return True

    async def get_attestation(self, signer, epoch):
        body = self.attestations.get((signer, epoch))
        return json.loads(json.dumps(body)) if body is not None else None

    async def put_attestation(self, signer, epoch, body, issued_ms=None):
        self.attestations.setdefault((signer, epoch), json.loads(json.dumps(body)))
        self.attestation_ms.setdefault((signer, epoch), int(time.time() * 1000)
                                       if issued_ms is None else int(issued_ms))

    async def attestation_issued_ms(self, signer, epoch):
        if (signer, epoch) not in self.attestations:
            return None
        return self.attestation_ms.get((signer, epoch))

    async def seal(self, signer, epoch, cut_ms):
        self.attestation_ms.setdefault((signer, epoch), int(cut_ms))

    async def drop_carry(self, signer, epoch):
        frame = _tx.get()
        if frame is None or frame["store"] is not self:
            raise RuntimeError("carry rows are dropped inside the seal's transaction")
        if (signer, epoch) in self.attestation_ms:
            raise RuntimeError(f"epoch {epoch} is sealed: its carry is kept")
        keep = [r for r in self.ledger if not (r.epoch == epoch and r.kind == "carry")]
        n, self.ledger = len(self.ledger) - len(keep), keep
        return n

    async def sealed_ms(self, signer, epoch):
        return self.attestation_ms.get((signer, epoch))

    async def latest_seal_ms(self, signer):
        epoch = await self.last_sealed_epoch(signer)
        return self.attestation_ms[(signer, epoch)] if epoch is not None else None

    async def last_sealed_epoch(self, signer):
        return max((e for s, e in self.attestation_ms if s == signer), default=None)

    async def put_reverify(self, row):
        key = (row.claim_id, row.validator)
        cur = self.reverify.get(key)
        self.reverify[key] = replace(row, id=cur.id if cur else next(self._ids))

    async def add_reverify_pull(self, validator, epoch, at_ms):
        self.pulls.append((validator, epoch, at_ms))

    async def reverify_claims(self, validator, epoch):
        return {cid for (cid, v), r in self.reverify.items() if v == validator and r.epoch == epoch}

    async def add_reverify_report(self, validator, epoch, at_ms):
        self.reports.setdefault((validator, epoch), at_ms)

    async def reverify_reports(self, epochs):
        wanted = set(epochs)
        return {k: v for k, v in self.reports.items() if k[1] in wanted}

    async def last_attestation_epoch(self, signer):
        return max((e for s, e in self.attestations if s == signer), default=None)

    async def get_event(self, event_id):
        e = self.events.get(event_id)
        return replace(e) if e else None

    async def event_by_identity(self, keys, fingerprint):
        keys = set(keys)
        return next((replace(e) for e in self.events.values()
                     if e.identity_key in keys or e.fingerprint == fingerprint), None)

    async def event_candidates(self, date_lo, date_hi, actor_keys, event_type, event_date):
        keys = set(actor_keys)
        return [replace(e) for e in self.events.values()
                if date_lo <= e.event_date <= date_hi
                and (keys & set(e.actor_keys)
                     or (e.event_type == event_type
                         and abs((e.event_date - event_date).days) <= 1))]

    async def add_event(self, rec):
        if any(e.fingerprint == rec.fingerprint
               or (rec.identity_key and e.identity_key == rec.identity_key)
               for e in self.events.values()):
            raise DuplicateEvent(rec.fingerprint)
        rec = replace(rec, id=next(self._ids))
        self.events[rec.id] = rec
        self.event_added_ms[rec.id] = time.time_ns() // 1_000_000
        return replace(rec)

    async def events_by_source(self, source_url, event_type, event_date):
        ids = {c.event_id for c in self.claims.values()
               if c.source_url == source_url and c.event_id is not None and c.status != "rejected"}
        return [replace(e) for i, e in sorted(self.events.items()) if i in ids
                and e.event_type == event_type and e.event_date == event_date]

    def _dead(self, event_id, before_ms):
        e = self.events.get(event_id)
        return (e is not None and e.confidence == "line"
                and self.event_added_ms.get(event_id, 0) < before_ms
                and not any(c.event_id == event_id and c.status != "rejected"
                            for c in self.claims.values())
                and not any(r.event_id == event_id for r in self.ledger)
                and not any(r.event_id == event_id for r in self.source_event_rows))

    async def dead_lines(self, before_ms, limit=500):
        return [i for i in sorted(self.events) if self._dead(i, before_ms)][:limit]

    async def drop_lines(self, event_ids):
        gone = [i for i in event_ids if self._dead(i, float("inf"))]
        for i in gone:
            del self.events[i]
            for cid, c in list(self.claims.items()):
                if c.event_id == i:
                    self.claims[cid] = replace(c, event_id=None)
        return len(gone)

    async def prize_docs(self, since_ms, probe=None):
        return [c.doc_hash for c in self.claims.values()
                if c.doc_hash and c.route in PRIZE_ROUTES and c.status in WON
                and c.received_ms >= since_ms]

    async def add_source_fetch(self, row):
        row = replace(row, id=next(self._ids))
        self.fetches.append(row)
        return replace(row)

    async def update_source_fetch(self, fetch_id, **fields):
        for i, f in enumerate(self.fetches):
            if f.id == fetch_id:
                self.fetches[i] = replace(f, **fields)

    async def source_fetches(self, claim_id):
        return [replace(f) for f in self.fetches if f.claim_id == claim_id]

    async def fetches_since(self, since_ms, limit=100_000):
        rows = [f for f in self.fetches if f.fetched_ms is not None and f.fetched_ms >= since_ms]
        return [replace(f) for f in rows[-limit:]]

    async def won_by_host(self, since_ms, hour_ms):
        from urllib.parse import urlsplit
        out: Dict[Tuple[str, str], List[int]] = {}
        for c in self.claims.values():
            if (c.status in WON and c.route in PRIZE_ROUTES and c.verified_ms is not None
                    and c.verified_ms >= since_ms):
                n = out.setdefault(((urlsplit(c.source_url).hostname or "").lower(),
                                    c.event_type), [0, 0])
                n[0] += c.verified_ms >= hour_ms
                n[1] += 1
        return [(h, t, a, b) for (h, t), (a, b) in out.items()]

    async def held_claims(self, limit=500):
        rows = [c for c in self.claims.values() if c.held and c.status in PASSED]
        return [replace(c) for c in sorted(rows, key=lambda c: c.id)[:limit]]

    async def add_judge_item(self, row):
        hit = await self.judge_item(row.key)
        if hit is not None:
            return hit
        row = replace(row, id=next(self._ids), task=json.loads(json.dumps(row.task)))
        self.judge_item_rows[row.id] = row
        return replace(row)

    async def judge_item(self, key):
        return next((replace(r) for r in self.judge_item_rows.values() if r.key == key), None)

    async def judge_items(self, status="pending", limit=500):
        rows = sorted((r for r in self.judge_item_rows.values() if r.status == status),
                      key=lambda r: r.id)
        return [replace(r) for r in rows[:limit]]

    async def update_judge_item(self, item_id, status):
        self.judge_item_rows[item_id] = replace(self.judge_item_rows[item_id], status=status)

    async def judge_items_count(self, hotkey, since_ms):
        return sum(1 for r in self.judge_item_rows.values()
                   if r.hotkey == hotkey and r.created_ms >= since_ms)

    async def add_judge_post(self, row):
        if any(p.item_id == row.item_id and p.validator == row.validator
               for p in self.judge_post_rows):
            return None
        row = replace(row, id=next(self._ids))
        self.judge_post_rows.append(row)
        return replace(row)

    async def judge_posts(self, item_ids):
        ids = set(item_ids)
        return [replace(p) for p in self.judge_post_rows if p.item_id in ids]

    async def judge_verdict(self, key):
        hit = self.verdicts.get(key)
        return dict(hit[1]) if hit else None

    async def add_judge_verdict(self, key, claim_id, record):
        self.verdicts.setdefault(key, (claim_id, dict(record)))

    async def claims_by_source(self, source_id, statuses):
        return [replace(c) for c in self.claims.values()
                if c.source_id == source_id and c.status in statuses]

    async def sources(self, domain=None):
        return [replace(r) for r in self.source_rows.values()
                if domain is None or r.domain == domain]

    async def get_source(self, source_id):
        r = self.source_rows.get(source_id)
        return replace(r) if r else None

    async def add_source(self, row):
        for r in self.source_rows.values():
            if (r.domain, r.host, r.path_rule) == (row.domain, row.host, row.path_rule):
                return replace(r)
        row = replace(row, id=next(self._ids))
        self.source_rows[row.id] = row
        return replace(row)

    async def update_source(self, source_id, **fields):
        self.source_rows[source_id] = replace(self.source_rows[source_id], **fields)

    async def get_owner(self, owner_id):
        o = self.owner_rows.get(owner_id)
        return replace(o) if o else None

    async def put_owner(self, row):
        self.owner_rows[row.id] = replace(row)

    async def source_trust(self, source_id=None):
        return [replace(r) for (sid, _), r in sorted(self.trust_rows.items())
                if source_id is None or sid == source_id]

    async def put_source_trust(self, rows):
        for r in rows:
            self.trust_rows[(r.source_id, r.category)] = replace(r)

    async def add_source_event(self, row):
        row = replace(row, id=next(self._ids))
        self.source_event_rows.append(row)
        return replace(row)

    async def source_events(self, source_id=None, event_id=None, claim_id=None):
        return [replace(r) for r in self.source_event_rows
                if (source_id is None or r.source_id == source_id)
                and (event_id is None or r.event_id == event_id)
                and (claim_id is None or r.claim_id == claim_id)]

    async def page_rules(self):
        return [replace(r) for r in self.rule_rows.values()]

    async def add_page_rule(self, row):
        cur = self.rule_rows.get(row.pattern)
        row = replace(row, id=cur.id if cur else next(self._ids))
        self.rule_rows[row.pattern] = row
        return replace(row)

    async def add_trust_log(self, row):
        self.log_rows.append(replace(row, id=next(self._ids)))

    async def trust_log(self, source_id=None, actor=None, since_ms=None):
        return [replace(r) for r in self.log_rows
                if (source_id is None or r.source_id == source_id)
                and (actor is None or r.actor == actor)
                and (since_ms is None or r.at_ms >= since_ms)]

    async def add_answer_items(self, rows):
        added = []
        for r in rows:
            key = (r.adapter, r.external_id)
            if key not in self.answer_rows:
                self.answer_rows[key] = replace(r, id=next(self._ids))
                added.append(replace(self.answer_rows[key]))
        return added

    async def answer_items(self, statuses, observed_since_ms=None, limit=500):
        rows = [r for r in self.answer_rows.values() if r.status in statuses
                and (observed_since_ms is None or r.observed_ms >= observed_since_ms)]
        return [replace(r) for r in sorted(rows, key=lambda r: r.id)[:limit]]

    async def update_answer_item(self, item_id, **fields):
        for key, r in self.answer_rows.items():
            if r.id == item_id:
                self.answer_rows[key] = replace(r, **fields)

    async def add_accuracy_check(self, row):
        key = (row.claim_id, row.kind, row.ref)
        if key in self.check_rows:
            return False
        self.check_rows[key] = replace(row, id=next(self._ids))
        return True

    async def accuracy_checks(self, since_ms, hotkeys=None, category=None):
        wanted = set(hotkeys) if hotkeys is not None else None
        return [replace(r) for r in self.check_rows.values() if r.at_ms >= since_ms
                and (wanted is None or r.hotkey in wanted)
                and (category is None or r.category == category)]

    async def put_miner_accuracy(self, rows):
        for r in rows:
            self.accuracy_rows[(r.hotkey, r.category)] = replace(r)

    async def miner_accuracy(self, hotkey=None):
        return [replace(r) for (hk, _), r in sorted(self.accuracy_rows.items())
                if hotkey is None or hk == hotkey]

    async def claims_by_settle(self, since_ms, until_ms, statuses):
        rows = [c for c in self.claims.values() if c.status in statuses
                and c.settle_ms is not None and since_ms <= c.settle_ms < until_ms]
        return [replace(c) for c in sorted(rows, key=lambda c: c.id)]

    async def event_stages(self, event_id):
        rows = [(stage, cid, at) for (eid, stage), (cid, at) in self.stage_rows.items()
                if eid == event_id]
        return sorted(rows, key=lambda r: r[2])

    async def list_events(self, q):
        rows = [e for e in self.events.values() if _event_matches(e, q)]
        rows.sort(key=lambda e: (e.first_seen_ms or 0, e.id), reverse=True)
        return [replace(e) for e in rows[:q.limit]]

    async def add_feed(self, row):
        row = replace(row, seq=len(self.feed_rows) + 1)
        self.feed_rows.append(row)
        return replace(row)

    async def feed(self, after_seq=0, until_ms=None, event_id=None, category=None,
                   event_type=None, entity_keys=(), limit=500):
        keys = set(entity_keys)
        out = []
        for r in self.feed_rows:
            if (r.seq <= after_seq or (until_ms is not None and r.at_ms > until_ms)
                    or event_id not in (None, r.event_id) or category not in (None, r.category)
                    or event_type not in (None, r.event_type)):
                continue
            if keys and not keys & set(self.events[r.event_id].actor_keys):
                continue
            out.append(replace(r))
            if len(out) >= limit:
                break
        return out

    async def api_key_owner(self, key_hash):
        return self.api_keys.get(key_hash)

    async def add_api_key(self, key_hash, owner_id):
        self.api_keys[key_hash] = owner_id

    async def revoke_api_keys(self, owner_id=None, key_hash=None):
        if not owner_id and not key_hash:
            return 0
        gone = [k for k, o in self.api_keys.items()
                if owner_id in (None, o) and key_hash in (None, k)]
        for k in gone:
            del self.api_keys[k]
        return len(gone)

    async def registry_entities(self):
        return [r for r in self.entity_rows.values() if r.origin == "registry"]

    async def put_entity(self, record):
        old = self.entity_rows.get(record.key)
        if old is None or refreshes(old, record):
            self.entity_rows[record.key] = record

    async def miner_stats(self, since_ms, until_ms, category=None):
        out: Dict[str, MinerStats] = {}
        ids = {}
        for c in self.claims.values():
            if not since_ms <= c.received_ms < until_ms or category not in (None, c.category):
                continue
            s = out.setdefault(c.hotkey, MinerStats())
            ids[c.id] = s
            s.claims += 1
            s.verified += c.verified_ms is not None
            if c.status in ("rejected", "clawed_back") and c.reason:
                s.rejected[c.reason] = s.rejected.get(c.reason, 0) + 1
        for r in self.ledger:
            s = ids.get(r.claim_id)
            if s is not None:
                kinds = s.points.setdefault(r.route or "", {})
                kinds[r.kind] = kinds.get(r.kind, 0.0) + r.points
        return out

    async def claims_page(self, until_ms, hotkey=None, route=None, status=None, category=None,
                          since_ms=None, before_id=None, limit=50, event_type=None, reason=None):
        rows = [c for c in self.claims.values() if _visible(c, until_ms)
                and hotkey in (None, c.hotkey) and route in (None, c.route)
                and status in (None, c.status) and category in (None, c.category)
                and event_type in (None, c.event_type) and reason in (None, c.reason)
                and (since_ms is None or c.received_ms >= since_ms)
                and (before_id is None or c.id < before_id)]
        return [replace(c) for c in sorted(rows, key=lambda c: -c.id)[:limit]]

    async def banded_claims(self, since_ms):
        return [replace(c) for c in sorted(self.claims.values(), key=lambda c: c.id)
                if c.trust_band is not None and (c.verified_ms or 0) >= since_ms]

    async def claims_of_types(self, since_ms, event_types):
        wanted = set(event_types)
        return [replace(c) for c in sorted(self.claims.values(), key=lambda c: c.id)
                if c.event_type in wanted and c.received_ms >= since_ms]

    async def ledger_for_claims(self, claim_ids):
        wanted = set(claim_ids)
        return [replace(r) for r in self.ledger if r.claim_id in wanted]

    async def reverify_for_claims(self, claim_ids):
        wanted = set(claim_ids)
        return [replace(r) for (cid, _), r in sorted(self.reverify.items()) if cid in wanted]

    async def fetches_for_claims(self, claim_ids):
        wanted = set(claim_ids)
        return [replace(f) for f in self.fetches if f.claim_id in wanted]

    async def claims_by_ids(self, claim_ids):
        return [replace(self.claims[i]) for i in sorted(set(claim_ids)) if i in self.claims]

    async def judge_items_for_claims(self, claim_ids):
        wanted = set(claim_ids)
        return [replace(r) for _, r in sorted(self.judge_item_rows.items())
                if r.claim_id in wanted]

    async def judge_posts_since(self, since_ms):
        return [(self.judge_item_rows[p.item_id].claim_id, replace(p))
                for p in self.judge_post_rows
                if p.posted_ms >= since_ms and p.item_id in self.judge_item_rows]

    async def candidates_for_scoops(self, scoop_ids):
        wanted = set(scoop_ids)
        return [replace(c) for _, c in sorted(self.candidate_rows.items())
                if c.scoop_id in wanted]

    async def reverify_reports_since(self, since_ms):
        return {k: v for k, v in self.reports.items() if v >= since_ms}

    async def reverify_in_epochs(self, epochs):
        wanted = set(epochs)
        return [replace(r) for _, r in sorted(self.reverify.items()) if r.epoch in wanted]
