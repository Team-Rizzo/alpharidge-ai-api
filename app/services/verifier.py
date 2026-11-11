"""Verifier: fetch each claim's source on arrival, then after novelty run the shared
deterministic check and queue the claim for the listed validators' page verdicts.

Only hashes, offsets and page metadata are stored. Page text is held in memory for the
URL cache window and never written to the database. Failed fetches are retried, not cached.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from alpharidge_ai.judge.prompt import failed as judge_failed
from alpharidge_ai.schema import Claim, category_of, judged
from alpharidge_ai.verify.check import (
    REJECTED, UNREAD, CheckResult, check_claim, check_scoop,
)
from alpharidge_ai.verify.fetch import BLOCKED, FETCH_FAILED, FetchConfig, FetchedPage, Fetcher
from app.config import EVENT_LANE
from app.services import (budget, intake, judge as judges, ledger, novelty, page_flags, points,
                          trust)
from app.services.answer_key import releases
from app.services.entities import EntityIndex, PlaceIndex, shared_index
from app.services.store import PRIZE_ROUTES, ClaimRow, EventRecord, Job, SourceFetchRow, Store
from app.utils.clock import DAY_MS, ms as _ms, now_ms as _now_ms

FETCH_MAX_BYTES = 2_000_000
FETCH_MAX_REDIRECTS = 3
FETCH_RETRY_DELAY_S = 2
FETCH_PACE_MAX_WAIT_S = 1
FETCH_REQUEST_S = 60
URL_CACHE_S = 3600
PAGE_CACHE_MAX_BYTES = 512_000_000
# Fetch attempts before a claim is rejected, FETCH_REQUEST_S apart.
FETCH_TRIES = 3
# A claim waiting for the validators' page verdicts is looked at again this long after.
JUDGE_WAIT_S = 10
# A claim no verdict reached within the `judge_failing` alarm's wait is asked again this often,
# so the claims behind it are still reached.
JUDGE_IDLE_WAIT_S = 300
# Judge outcomes other than a pass one owner may get per page and type in a UTC day; later
# claims are turned away before the judge is asked.
JUDGE_REPEAT_MAX = 2

logger = logging.getLogger(__name__)

PENDING = "pending"
# Claims still waiting on their page.
OPEN = ("queued", "novelty")


class PageRules(Protocol):
    def page_excluded(self, url: str, page: FetchedPage) -> Optional[str]: ...


class PageCache:
    """Pages fetched successfully, by URL for a short window, without their HTML."""

    def __init__(self, ttl_s: float, max_items: int = 5000,
                 max_bytes: int = PAGE_CACHE_MAX_BYTES):
        self.ttl_s, self.max_items, self.max_bytes = ttl_s, max_items, max_bytes
        self._pages: Dict[str, Tuple[float, int, FetchedPage, int]] = {}
        self._bytes = 0

    @staticmethod
    def _size(page: FetchedPage) -> int:
        return len(page.text.encode()) + len(page.title.encode()) + 512

    def drop(self, url: str) -> None:
        self._drop(url)

    def _drop(self, url: str) -> None:
        hit = self._pages.pop(url, None)
        if hit is not None:
            self._bytes -= hit[3]

    def get(self, url: str, received_ms: int) -> Optional[FetchedPage]:
        """The page, when it was fetched after the claim arrived."""
        hit = self._pages.get(url)
        if hit is None or hit[0] < time.monotonic():
            self._drop(url)
            return None
        return hit[2] if hit[1] >= received_ms else None

    def put(self, url: str, page: FetchedPage, fetched_ms: int) -> None:
        if not page.ok:
            return
        self._drop(url)
        page = replace(page, html="")
        size = self._size(page)
        if size > self.max_bytes:
            return
        now = time.monotonic()
        # Entries are kept in expiry order.
        while self._pages and (next(iter(self._pages.values()))[0] < now
                               or len(self._pages) >= self.max_items
                               or self._bytes + size > self.max_bytes):
            self._drop(next(iter(self._pages)))
        self._pages[url] = (now + self.ttl_s, fetched_ms, page, size)
        self._bytes += size


def check_public_only(mock: bool) -> None:
    """Refuse fetches to private addresses except on a mock network."""
    if EVENT_LANE["FETCH_PUBLIC_ONLY"]:
        return
    if not mock:
        raise RuntimeError("EL_FETCH_PUBLIC_ONLY=false is only allowed with SUBTENSOR_NETWORK=mock")
    logger.warning("[CONFIG] WARNING: EL_FETCH_PUBLIC_ONLY=false, fetches may reach private "
                   "addresses")


def fetch_config() -> FetchConfig:
    return FetchConfig(
        user_agent=EVENT_LANE["USER_AGENT"], timeout_s=EVENT_LANE["FETCH_TIMEOUT_S"],
        max_bytes=FETCH_MAX_BYTES, max_redirects=FETCH_MAX_REDIRECTS,
        retries=EVENT_LANE["FETCH_RETRY"], retry_delay_s=FETCH_RETRY_DELAY_S,
        domain_rps=EVENT_LANE["DOMAIN_RPS"], public_only=EVENT_LANE["FETCH_PUBLIC_ONLY"],
        pace_max_wait_s=FETCH_PACE_MAX_WAIT_S)


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _age(row: ClaimRow, page: Optional[FetchedPage] = None) -> float:
    """The age factor pay would use, from the cached page's publish date when there is one."""
    return _age_of(row.payload.get("event_date"), row.received_ms, page)


def _age_of(event_date, received_ms: int, page: Optional[FetchedPage] = None) -> float:
    try:
        return points.age(date.fromisoformat(str(event_date)[:10]), received_ms,
                          _ms(page.published_at) if page else None)
    except (AttributeError, ValueError):
        return 0.0


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value):
    """Page-derived strings without NUL and other control characters, which the database
    refuses."""
    if isinstance(value, str):
        return _CONTROL.sub("", value)
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _failed(row: ClaimRow, rec: SourceFetchRow) -> FetchedPage:
    return FetchedPage(url=row.source_url, final_url=rec.final_url or "", status=rec.http_status,
                       reason=rec.reason or "fetch_failed",
                       unread=ledger.unread_fetch(rec))


class Verifier:
    def __init__(self, fetcher: Optional[Fetcher] = None, cache: Optional[PageCache] = None,
                 panel=None, rules: Optional[PageRules] = None,
                 entities: Optional[EntityIndex] = None, places: Optional[PlaceIndex] = None,
                 registry: Optional[trust.TrustRegistry] = None):
        self.fetcher = fetcher or Fetcher(fetch_config())
        self.cache = cache or PageCache(URL_CACHE_S)
        # Tests only: stands in for the listed validators.
        self.panel = panel
        self.registry = registry or trust.TrustRegistry(filings=trust.Filings(self.fetcher))
        self.rules = rules or self.registry
        self.entities = entities
        self.places = places or PlaceIndex()
        self._requested: Dict[Tuple[str, int], int] = {}
        self._judge_wait: Dict[int, int] = {}
        self._judge_since: Dict[int, int] = {}

    async def _record(self, store: Store, row: ClaimRow, page: FetchedPage,
                      at_ms: Optional[int] = None) -> SourceFetchRow:
        check = ({"page": page_flags.signals(page.html, page.final_url or page.url)}
                 if page.ok and page.html else None)
        if page.unread:
            check = {UNREAD: True}
        at_ms = at_ms or _now_ms()
        try:
            return await store.add_source_fetch(SourceFetchRow(
                claim_id=row.id, verdict=PENDING if page.ok else REJECTED,
                http_status=page.status, content_hash=page.content_hash or None,
                final_url=_clean(page.final_url) or None,
                canonical_url=_clean(page.canonical_url) or None,
                published_ms=_ms(page.published_at), bytes=page.bytes,
                reason=page.reason, fetched_ms=at_ms, check=_clean(check)))
        except Exception as e:
            # A page that cannot be stored counts as a failed fetch, so the claim moves on.
            logger.warning(f"Fetch row of claim {row.id} not stored: {e!r}")
            self.cache.drop(row.source_url)
            return await store.add_source_fetch(SourceFetchRow(
                claim_id=row.id, verdict=REJECTED, http_status=page.status,
                reason=FETCH_FAILED, fetched_ms=at_ms, check={"gave_up": True}))

    async def fetch(self, store: Store, row: ClaimRow, fresh: bool = False,
                    now_ms: Optional[int] = None,
                    open_only: bool = False) -> Tuple[FetchedPage, Optional[SourceFetchRow]]:
        """The claim's page, from the cache when fetched after the claim arrived, with its
        SourceFetch row. With `open_only`, a claim that closed during the fetch gets no row."""
        page = None if fresh else self.cache.get(row.source_url, row.received_ms)
        if page is None:
            at = now_ms or _now_ms()
            page = await self.fetcher.fetch(row.source_url)
            self.cache.put(row.source_url, page, at)
            if open_only:
                row = await store.get_claim(row.id)
                if row is None or row.status not in OPEN:
                    return page, None
            return page, await self._record(store, row, page, at)
        rec = next((f for f in reversed(await store.source_fetches(row.id))
                    if f.content_hash == page.content_hash), None)
        return page, rec or await self._record(store, row, page)

    def check(self, claim: Claim, page: FetchedPage, received: datetime,
              scoop: bool = False) -> CheckResult:
        """The deterministic check of a page, without the judge; a scoop's may be unread."""
        excluded = self.rules.page_excluded(page.final_url or page.url, page)
        if scoop:
            return check_scoop(claim, page, received, excluded=excluded)
        return check_claim(claim, page, received, excluded=excluded)

    def index(self) -> EntityIndex:
        if self.entities is None:
            self.entities = shared_index()
        return self.entities

    async def page_verdict(self, store: Store, row: ClaimRow, claim: Claim, page: FetchedPage,
                           now_ms: int, task: dict, by_hotkey: bool = True) -> Optional[dict]:
        """The validators' combined page verdict, or None while it must wait. A claim the
        judge does not pass but finds another open type or stage for is judged again as that;
        it is unclear when that type cannot hold it. On a filer's own 8-K the filing's items
        go with it."""
        since = now_ms - 7 * DAY_MS
        stats = (await store.hotkey_stats(since, [row.hotkey])).get(row.hotkey)
        passes = bool(stats and stats.passes)
        url = page.final_url or claim.source_url

        async def ask(c: Claim, t: dict) -> Optional[dict]:
            filing = await self.registry.filings.report(c.actors, url, c.event_type)
            return await judges.page_verdict(store, c, page, row.id, row.hotkey, passes, now_ms,
                                             t, filing=filing, by_hotkey=by_hotkey,
                                             panel=self.panel)

        v = await ask(claim, task)
        key = judges.retype_to(v, claim) if v else None
        if not key:
            return v
        retyped = {"kind": key, "first": v["outcome"]}
        other = judged(claim, {"retyped": retyped})
        if other is None or intake.off_reason(other):
            return {**v, "outcome": judges.UNCLEAR, "retyped": retyped}
        again = await ask(other, {**task, "judged_as": key})
        return {**again, "retyped": retyped} if again else None

    async def repeats(self, store: Store, row: ClaimRow, doc_hash: str, now_ms: int) -> int:
        """The owner's (coldkey, else hotkey) non-pass judge outcomes today on this page and
        type."""
        day = now_ms - now_ms % DAY_MS
        return await store.judge_repeats(row.coldkey or row.hotkey, row.event_type, doc_hash,
                                         day - DAY_MS, day, row.id,
                                         ("judge_reject", "judge_unclear"))

    async def _near(self, store: Store, claim: Claim, row: ClaimRow) -> List[EventRecord]:
        own = [i for i in (row.event_id,) if i is not None]
        return await novelty.near_events(store, claim, self.index(), self.places, own)

    def looked(self, claim_id: int, now_ms: int) -> bool:
        """True while the claim's last fetch request is under FETCH_REQUEST_S old."""
        last = self._requested.get((intake.FETCH_JOB, claim_id))
        return last is not None and now_ms - last < FETCH_REQUEST_S * 1000

    def _due(self, kind: str, row: ClaimRow, now_ms: int) -> bool:
        """At most one look per claim and kind every FETCH_REQUEST_S."""
        last = self._requested.get((kind, row.id))
        if last is not None and now_ms - last < FETCH_REQUEST_S * 1000:
            return False
        if len(self._requested) >= self.cache.max_items:
            self._requested.clear()
        self._requested[(kind, row.id)] = now_ms
        return True

    async def _page(self, store: Store, row: ClaimRow,
                    now_ms: int) -> Optional[Tuple[FetchedPage, SourceFetchRow]]:
        """The claim's page, or its last failure once every try has failed; None while waiting.
        A failed fetch is retried FETCH_REQUEST_S after it; a blocked page is not retried."""
        if self.cache.get(row.source_url, row.received_ms) is not None:
            return await self.fetch(store, row)
        if not self._due(intake.FETCH_JOB, row, now_ms):
            return None
        failed = [f for f in await store.source_fetches(row.id) if not f.content_hash]
        if failed and (len(failed) >= FETCH_TRIES or failed[-1].reason == BLOCKED
                       or (failed[-1].check or {}).get("gave_up")):
            return _failed(row, failed[-1]), failed[-1]
        if not failed or now_ms >= failed[-1].fetched_ms + FETCH_REQUEST_S * 1000:
            await store.add_job_once(intake.FETCH_JOB, row.id, now_ms)
        return None

    async def _untrusted_reason(self, store: Store, claim: Claim, row: ClaimRow,
                                now_ms: int) -> Optional[str]:
        """What resolve would reject an untrusted line head for, or None when it may be held
        as a scoop."""
        if claim.event_type in releases.NO_SCOOP_TYPES:
            if releases.get_book().before(claim, row.received_ms, self.index()):
                return "pre_release"
            return "low_trust"
        if not EVENT_LANE["SCOOPS"] or await budget.scoop_limited(store, row, now_ms):
            return "scoop_limit"
        return None

    async def _retyped(self, store: Store, claim: Claim, page: FetchedPage, source, url: str,
                       received: datetime, now_ms: int, fields: dict) -> CheckResult:
        """A claim the judge retyped: the fixed check again, then its novelty as that type.
        The stored claim becomes the retyped one; the signed body stays the miner's."""
        result = self.check(claim, page, received)
        if not result.passed:
            return result
        category = category_of(claim.event_type).value
        ct = await trust.of_claim(store, self.registry, self.index(), claim, source, category,
                                  url)
        n = await novelty.place(store, claim, self.index(), self.places, now_ms, ct.t, ct.filer)
        fields.update(payload=claim.model_dump(mode="json", exclude_none=True),
                      event_type=claim.event_type.value, category=category, novelty=n.state,
                      route=n.route, event_id=n.event_id)
        if n.state == novelty.KNOWN and n.route is None:
            return replace(result, verdict=REJECTED, reason=n.reason or "known_event")
        return result

    async def _filed_by_subject(self, store: Store, row: ClaimRow, claim: Claim,
                                now_ms: int) -> ClaimRow:
        """A new claim on a filing its subject made takes the event route, as novelty gives it
        once the filer is confirmed; a claim placed before that could be confirmed gets it here."""
        source = await trust.resolve(store, row.source_url, now_ms)
        ct = await trust.of_claim(store, self.registry, self.index(), claim, source, row.category,
                                  row.source_url)
        if ct.t < trust.PRIZE_TRUST_MIN or not await store.update_claim(row.id, "novelty",
                                                                          route="event"):
            return row
        return await store.get_claim(row.id) or row

    async def verify(self, store: Store, row: ClaimRow, now_ms: int,
                     near: Optional[Sequence[EventRecord]] = None) -> ClaimRow:
        """novelty -> fetched, or on to rejected when the check fails."""
        if row.status != "novelty" or not row.route:
            return row
        if self._judge_wait.get(row.id, 0) > now_ms:
            return row
        fetched = await self._page(store, row, now_ms)
        if fetched is None:
            return row
        self._requested.pop((intake.FETCH_JOB, row.id), None)
        claim = Claim.model_validate(row.payload)
        await self.registry.refresh(store)
        if (row.route == "scoop" and row.novelty == novelty.NEW
                and trust.filing_of(row.source_url) is not None):
            row = await self._filed_by_subject(store, row, claim, now_ms)
        page, rec = fetched
        url = (page.ok and page.final_url) or row.source_url
        source = await trust.resolve(store, url, now_ms)
        received = _utc(row.received_ms)
        result = self.check(claim, page, received, scoop=row.route == "scoop")

        fields = {"source_id": source.id}
        contrastive = False
        if result.passed and row.route in PRIZE_ROUTES:
            near = await self._near(store, claim, row) if near is None else near
            hit = novelty.contrastive(page.text, [e for e in near if e.id != row.event_id],
                                     novelty.subject_names(claim, self.index()))
            ev = await store.get_event(hit) if hit is not None else None
            if ev is not None:
                contrastive = True
                fields.update(novelty=novelty.KNOWN, route=None, event_id=ev.id)
                result = replace(result, verdict=REJECTED, reason="contrastive_known")

        if page.ok:
            fields["doc_hash"] = novelty.doc_hash(page.canonical_url or page.final_url
                                                  or row.source_url, page.text)
        ct = None
        if result.passed and row.route in PRIZE_ROUTES and trust.filing_of(url) is None:
            # An untrusted page that cannot be held as a scoop is not judged.
            ct = await trust.of_claim(store, self.registry, self.index(), claim, source,
                                      row.category, url)
            why = (await self._untrusted_reason(store, claim, row, now_ms)
                   if ct.t < trust.PRIZE_TRUST_MIN else None)
            if why:
                result = replace(result, verdict=REJECTED, reason=why)
        if result.unread and result.passed and (ct is None or ct.t >= trust.PRIZE_TRUST_MIN):
            result = replace(result, verdict=REJECTED, reason=BLOCKED)
        verdict = None
        if result.passed and not result.unread and await self.repeats(
                store, row, fields["doc_hash"], now_ms) >= JUDGE_REPEAT_MAX:
            result = replace(result, verdict=REJECTED, reason="judge_repeat")
        # An unread scoop is held on its receipt; the claim on its confirming page is judged.
        if result.passed and not result.unread:
            task = judges.task_of(row, judges.CLAIM, row.source_url, row.received_ms,
                                  final_url=page.final_url or None)
            verdict = await self.page_verdict(store, row, claim, page, now_ms, task)
            if verdict is None:
                if len(self._judge_wait) >= self.cache.max_items:
                    self._judge_wait.clear()
                    self._judge_since.clear()
                since = self._judge_since.setdefault(row.id, now_ms)
                idle = now_ms - since >= 2 * judges.JUDGE_QUORUM_WAIT_S * 1000
                self._judge_wait[row.id] = now_ms + (
                    JUDGE_IDLE_WAIT_S if idle else JUDGE_WAIT_S) * 1000
                return row
            self._judge_wait.pop(row.id, None)
            self._judge_since.pop(row.id, None)
            if verdict["outcome"] != judges.PASS:
                why = judge_failed(verdict, judged(claim, verdict) or claim) if verdict.get(
                    "answers") else []
                result = replace(result, verdict=REJECTED, detail={"judge": why} if why else None,
                                 reason=verdict.get("reason") or f"judge_{verdict['outcome']}")
            elif verdict.get("retyped"):
                claim, ct = judged(claim, verdict), None
                result = await self._retyped(store, claim, page, source, url, received, now_ms,
                                             fields)

        await store.update_source_fetch(rec.id, verdict=result.verdict, reason=result.reason,
                                        spans=[list(s) for s in result.spans],
                                        check={**(rec.check or {}),
                                               "date_gate": result.date_gate,
                                               "detail": result.detail,
                                               "contrastive": contrastive,
                                               **({"judge": verdict} if verdict else {})})
        category = fields.get("category", row.category)
        ct = ct or await trust.of_claim(store, self.registry, self.index(), claim, source,
                                        category, url)
        fields.update(trust=ct.t, trust_band=ct.band)
        event_id = fields.get("event_id", row.event_id)
        if result.passed and event_id is not None:
            book = releases.get_book()
            early = claim.event_type in releases.NO_SCOOP_TYPES and (
                ct.t < trust.PRIZE_TRUST_MIN or book.before(claim, row.received_ms, self.index()))
            if not early:
                await trust.record(store, trust.FIRST, source, category, row.received_ms,
                                   event_id, row.id, _ms(page.published_at))
        row = await intake.transition(store, row, "fetched", **fields)
        if not result.passed:
            row = await intake.transition(store, row, "rejected", reason=result.reason,
                                          judged_ms=now_ms)
        return row


_default: Optional[Verifier] = None


def get_verifier() -> Verifier:
    global _default
    if _default is None:
        _default = Verifier()
    return _default


async def fetch_job(store: Store, job: Job) -> None:
    row = await store.get_claim(job.claim_id) if job.claim_id is not None else None
    if row is None or row.status not in OPEN:
        return
    now_ms = _now_ms()
    if row.status == "queued" and not await budget.within_budget(store, row, now_ms):
        return
    # Two jobs can fetch one claim; the later must not add a row after the check closed it.
    await get_verifier().fetch(store, row, open_only=True)


async def fetch_gave_up(store: Store, job: Job) -> None:
    """The claim's page is not fetched again."""
    row = await store.get_claim(job.claim_id) if job.claim_id is not None else None
    if row is not None and row.status in OPEN:
        await store.add_source_fetch(SourceFetchRow(
            claim_id=row.id, verdict=REJECTED, reason="fetch_failed", fetched_ms=_now_ms(),
            check={"gave_up": True}))


async def report_job(store: Store, job: Job) -> None:
    """A trusted report on an event another claim took: when its page passes the fixed check,
    its owner counts toward the event's `confirmed` label. The claim stays as it ended."""
    from app.services import product

    row = await store.get_claim(job.claim_id) if job.claim_id is not None else None
    ev = await store.get_event(row.event_id) if row and row.event_id is not None else None
    if ev is None or ev.first_claim_id in (None, row.id) or ev.confidence == product.CONFIRMED:
        return
    v, now_ms = get_verifier(), _now_ms()
    counted = await product.counted_owners(store, ev)
    if product.owner_key(await trust.resolve(store, row.source_url, now_ms)) in counted:
        return
    page = await v.fetcher.fetch(row.source_url)
    if not page.ok:
        return
    url = page.final_url or row.source_url
    source = await trust.resolve(store, url, now_ms)
    if product.owner_key(source) in counted:
        return
    await v.registry.refresh(store)
    claim = Claim.model_validate(row.payload)
    ct = await trust.of_claim(store, v.registry, v.index(), claim, source, row.category, url)
    if ct.t < trust.PRIZE_TRUST_MIN or not (
            ct.primary or await trust.counts_as_owner(store, source, row.category)):
        return
    if not v.check(claim, page, _utc(row.received_ms)).passed:
        return
    await trust.confirm(store, ev.id, source, row, _ms(page.published_at))
    await product.sync_events(store, [ev.id], now_ms, v.index())


async def verify_ready(store: Store, now_ms: int, limit: int = 50, scan: int = 500) -> int:
    """Checks up to `limit` claims whose page is cached; the rest wait for their fetch.
    Claims waiting for the page verdicts are left out of the scan and the count. Lines with the
    higher age factor go first, so older events reach the judges after fresh ones."""
    v, done = get_verifier(), 0
    waiting = [i for i, until in v._judge_wait.items() if until > now_ms]
    heads = await store.claim_heads("novelty", limit=scan, skip=waiting)
    # A line moves at its freshest claim's age and keeps its own receipt order.
    line = {h.id: h.event_id if h.event_id is not None else -h.id for h in heads}
    lead: Dict[int, float] = {}
    for h in heads:
        a = _age_of(h.event_date, h.received_ms, v.cache.get(h.source_url, h.received_ms))
        lead[line[h.id]] = max(lead.get(line[h.id], 0.0), a)
    # Only claims with a cached page, or due a fetch request, are read in full.
    order = [h.id for h in sorted(heads, key=lambda h: -lead[line[h.id]])
             if v.cache.get(h.source_url, h.received_ms) is not None
             or not v.looked(h.id, now_ms)]
    rows = {r.id: r for r in await store.claims_by_ids(order)} if order else {}
    for cid in order:
        row = rows.get(cid)
        if row is None:
            continue
        if done >= limit:
            break
        try:
            cached = v.cache.get(row.source_url, row.received_ms) is not None
            out = await v.verify(store, row, now_ms)
            done += cached and out.status != row.status
        except intake.InvalidTransition:
            pass
        except Exception as e:
            logger.warning(f"Verify of claim {row.id} failed: {e!r}")
    return done
