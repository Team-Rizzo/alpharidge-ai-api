"""Resolve checked claims: the head of each line is verified, its event written and paid.
Scoop claims wait for confirmation.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional, Sequence, Tuple

from alpharidge_ai.schema import TYPE_SPEC, Claim
from alpharidge_ai.verify.check import SUPPORTED, UNREAD
from app.services import budget, intake, ledger, line, novelty, points, product, scoops, trust
from app.services.answer_key import releases
from app.services.entities import EntityIndex, PlaceIndex, shared_index
from app.services.store import (
    PRIZE_ROUTES, ClaimRow, EventFactRow, EventRecord, SourceFetchRow, SourceRow, Store,
)
from app.utils import block
from app.utils.clock import HOUR_MS

LINE_WAIT_S = 600
# A verified prize claim whose pay did not commit (its transaction could not start, or timed
# out) is paid again this long after it was verified; a pay still on its way is not raced.
PAY_RETRY_MS = 10 * 60 * 1000
# Verified claims read per query when held scoops are closed.
CLOSE_PAGE = 500

logger = logging.getLogger(__name__)

REPORTED, UNVERIFIED, LINE = "reported", "unverified", "line"


class Resolver:
    def __init__(self, entities: Optional[EntityIndex] = None,
                 places: Optional[PlaceIndex] = None,
                 registry: Optional[trust.TrustRegistry] = None):
        self.entities = entities
        self.places = places or PlaceIndex()
        self.registry = registry or trust.TrustRegistry()

    def index(self) -> EntityIndex:
        if self.entities is None:
            self.entities = shared_index()
        return self.entities

    # Inputs frozen at verification

    async def _trust(self, store: Store, row: ClaimRow) -> Tuple[float, Optional[SourceRow]]:
        """(T, source), as frozen at verification."""
        source = await store.get_source(row.source_id) if row.source_id is not None else None
        if source is None or row.trust is None:
            return 0.0, source
        return row.trust, source

    # Transitions

    async def _verified(self, store: Store, row: ClaimRow, now_ms: int, **fields) -> ClaimRow:
        if row.status == "verified":
            if fields:
                await store.update_claim(row.id, "verified", **fields)
            return await store.get_claim(row.id)
        return await intake.transition(store, row, "verified", verified_ms=now_ms,
                                       judged_ms=now_ms, **fields)

    async def _reject(self, store: Store, row: ClaimRow, reason: str, now_ms: int) -> ClaimRow:
        return await intake.transition(store, row, "rejected", reason=reason, judged_ms=now_ms)

    async def _reject_with_debt(self, store: Store, row: ClaimRow, reason: str, now_ms: int,
                                head: int) -> ClaimRow:
        """Reject a held scoop and write its debt in one transaction under the ledger lock:
        if either fails the scoop stays verified, and is closed again on a later pass."""
        async with store.transaction() as tx:
            await tx.lock_ledger()
            row = await self._reject(tx, row, reason, now_ms)
            await points.debt(tx, row, head)
        return row

    async def _waiting(self, store: Store, row: ClaimRow, now_ms: int) -> bool:
        """True while a claim ahead in the same line has not been checked yet."""
        cutoff = now_ms - int(LINE_WAIT_S * 1000)
        others = [c for c in await store.claims_for_event(row.event_id, ("novelty",))
                  if c.id != row.id and c.received_ms >= cutoff]
        if not others:
            return False
        return (await line.in_line_order(store, [row, *others]))[0].id != row.id

    async def _store_facts(self, store: Store, ev: EventRecord, row: ClaimRow, claim: Claim,
                           pending: bool = False) -> None:
        """Store the winning claim's facts. Pending facts wait for a confirming source and do
        not fill their slot."""
        spec = TYPE_SPEC[claim.event_type]
        rows = []
        for f in claim.facts:
            fact = EventFactRow(event_id=ev.id, field=f.field, value=f.value, unit=f.unit,
                                claim_id=row.id, doc_hash=row.doc_hash, source_url=row.source_url)
            if f.field not in spec.slots:
                rows.append(replace(fact, status="offschema"))
            else:
                status = "pending" if pending else "accepted"
                rows.append(replace(fact, slot=f.field, status=status))
        if rows:
            await store.add_facts(rows)

    # Routes

    async def run(self, store: Store, now_ms: int, head: Optional[int] = None,
                  limit: int = 200) -> int:
        await self.registry.refresh(store)
        rows = await store.claims_by_status("fetched", limit=limit)
        done, touched = 0, set()
        for row in await line.in_line_order(store, rows):
            row = await store.get_claim(row.id)
            if row is None or row.status != "fetched":
                continue
            try:
                out = await self.resolve(store, row, now_ms, head)
                done += out.status != "fetched"
                if out.status in ("verified", "provisional") and out.event_id is not None:
                    touched.add(out.event_id)
            except intake.InvalidTransition:
                pass
            except Exception as e:
                logger.warning(f"Resolve of claim {row.id} failed: {e!r}")
        await self.close_windows(store, now_ms, head)
        await product.sync_events(store, touched, now_ms, self.index())
        return done

    async def resolve(self, store: Store, row: ClaimRow, now_ms: int,
                      head: Optional[int] = None) -> ClaimRow:
        checked = checked_fetch(await store.source_fetches(row.id))
        if checked is not None and checked.verdict != SUPPORTED:
            # Its check failed and the move to rejected did not commit: it is not placed.
            return await self._reject(store, row, checked.reason or "unsupported", now_ms)
        claim = Claim.model_validate(row.payload)
        ev = await store.get_event(row.event_id) if row.event_id is not None else None
        if ev is None or row.route not in PRIZE_ROUTES:
            return await self._reject(store, row, "unsupported", now_ms)
        if await self._waiting(store, row, now_ms):
            return row
        if ev.confidence == LINE:
            return await self._open(store, row, claim, ev, now_ms, head)
        if row.route == "event" and novelty._prize_open(ev):
            taken = await self._take_prize(store, row, claim, ev, now_ms, head)
            if taken is not None:
                return taken
        if row.route == "scoop" and novelty._prize_open(ev) and await novelty.contests(
                store, claim, ev, self.index()):
            return await self._rival(store, row, claim, ev, now_ms)
        reason = "known_event" if await self._scooped(store, ev) else "lost_line"
        return await self._reject(store, row, reason, now_ms)

    async def _open(self, store: Store, row: ClaimRow, claim: Claim, ev: EventRecord,
                    now_ms: int, head: Optional[int]) -> ClaimRow:
        """The head of an open line: it wins the event (or opens it, as a scoop)."""
        if row.doc_hash and not await novelty.doc_prize_open(
                store, row.doc_hash, now_ms, self.registry.is_listing(row.source_url)):
            return await self._reject(store, row, "doc_prize_used", now_ms)
        t, _ = await self._trust(store, row)
        route = "event" if t >= trust.PRIZE_TRUST_MIN else "scoop"
        if claim.event_type in releases.NO_SCOOP_TYPES:
            if releases.get_book().before(claim, row.received_ms, self.index()):
                return await self._reject(store, row, "pre_release", now_ms)
            if route == "scoop":
                return await self._reject(store, row, "low_trust", now_ms)
        if route == "scoop" and await budget.scoop_limited(store, row, now_ms):
            return await self._reject(store, row, "scoop_limit", now_ms)
        if route == "event":
            await store.update_event(ev.id, confidence=REPORTED, first_claim_id=row.id)
        else:
            await store.update_event(ev.id, confidence=UNVERIFIED,
                                     first_seen_ms=row.received_ms)
        await self._first_stage(store, ev, row, claim, now_ms)
        await self._store_facts(store, ev, row, claim, pending=route == "scoop")
        row = await self._verified(store, row, now_ms, route=route)
        if route == "scoop":
            return row
        a = await points.claim_age(store, row, claim.event_date)
        return await points.pay(store, row, route, points.factors(
            claim.event_type, route, t, a, received_ms=row.received_ms), now_ms, head)

    async def _rival(self, store: Store, row: ClaimRow, claim: Claim, ev: EventRecord,
                     now_ms: int) -> ClaimRow:
        """A scoop on another outcome than the scoops held on the event, held beside them;
        the first confirmed takes the event and the contested rule pays it."""
        t, _ = await self._trust(store, row)
        if t >= trust.PRIZE_TRUST_MIN:
            return await self._reject(store, row, "known_event", now_ms)
        if row.doc_hash and not await novelty.doc_prize_open(
                store, row.doc_hash, now_ms, self.registry.is_listing(row.source_url)):
            return await self._reject(store, row, "doc_prize_used", now_ms)
        if await budget.scoop_limited(store, row, now_ms):
            return await self._reject(store, row, "scoop_limit", now_ms)
        await self._store_facts(store, ev, row, claim, pending=True)
        return await self._verified(store, row, now_ms, route="scoop")

    async def _first_stage(self, store: Store, ev: EventRecord, row: ClaimRow, claim: Claim,
                           now_ms: int) -> None:
        """The opening stage, recorded unpaid when the claim leaves it out."""
        if claim.stage is not None:
            await store.add_stage(ev.id, claim.stage.value, row.id, now_ms)
            return
        stages = TYPE_SPEC[claim.event_type].stages
        if stages:
            await store.add_stage(ev.id, stages[0].value, None, now_ms)
            await store.update_event(ev.id, stage=stages[0].value)

    async def _take_prize(self, store: Store, row: ClaimRow, claim: Claim, ev: EventRecord,
                          now_ms: int, head: Optional[int]) -> Optional[ClaimRow]:
        """A trusted page takes the prize of a scoop-opened event. It waits while a scoop ahead
        of it in line is held; None when the page does not qualify."""
        held = [c for c in await store.claims_for_event(ev.id, ("verified",)) if c.route == "scoop"]
        order = [c.id for c in await line.in_line_order(store, [row, *held])]
        # Only scoops ahead of it in line hold it; its page is checked as their confirmation.
        held = [c for c in held if order.index(c.id) < order.index(row.id)]
        if held:
            for s in held:
                await scoops.submit(store, s.id, row.source_url, scoops.API_FINDER, now_ms)
            return row
        t, source = await self._trust(store, row)
        if t < trust.PRIZE_TRUST_MIN or source is None:
            return None
        if row.doc_hash and not await novelty.doc_prize_open(
                store, row.doc_hash, now_ms, self.registry.is_listing(row.source_url)):
            return None
        await store.update_event(ev.id, confidence=REPORTED, first_claim_id=row.id)
        if claim.stage is not None and claim.stage.value != ev.stage:
            await store.add_stage(ev.id, claim.stage.value, row.id, now_ms)
            await store.update_event(ev.id, stage=claim.stage.value)
        await self._store_facts(store, ev, row, claim)
        row = await self._verified(store, row, now_ms)
        a = await points.claim_age(store, row, claim.event_date)
        return await points.pay(store, row, "event", points.factors(
            claim.event_type, "event", t, a, received_ms=row.received_ms), now_ms, head)

    async def _scooped(self, store: Store, ev: EventRecord) -> bool:
        first = await store.get_claim(ev.first_claim_id) if ev.first_claim_id else None
        return first is not None and first.route == "scoop"

    async def confirm_scoop(self, store: Store, scoop: ClaimRow, found: Claim, confirm: dict,
                            t: float, now_ms: int, head: Optional[int] = None) -> ClaimRow:
        """A trusted page confirms a held scoop: SCOOP_PREMIUM x the tier price when it was published
        after the scoop's receipt, else 1x. The confirmation joins the scoop's judge record before
        pay, so the signed leaf covers it."""
        fetch = ledger.judged_fetch(await store.source_fetches(scoop.id))
        record = ledger.judge_record([fetch]) if fetch else None
        if record is None and ledger.unread_fetch(fetch):
            # An unread scoop had no intake judge; the confirming page's judge stands in.
            record = {UNREAD: True}
        if record is None:
            raise intake.InvalidTransition(f"claim {scoop.id} has no judge record")
        await store.update_source_fetch(fetch.id, check={**(fetch.check or {}),
                                                         "judge": {**record, "confirm": confirm}})
        await store.update_facts(scoop.id, "accepted", [f.field for f in found.facts])
        ev = await store.get_event(scoop.event_id) if scoop.event_id is not None else None
        if ev is not None and ev.first_claim_id is None:
            await store.update_event(ev.id, confidence=REPORTED, first_claim_id=scoop.id)
        claim = Claim.model_validate(scoop.payload)
        a = await points.claim_age(store, scoop, claim.event_date)
        m = points.confirm_multiple(scoop, confirm)
        return await points.pay(store, scoop, "scoop",
                                points.factors(claim.event_type, "scoop", t, a, m,
                                               received_ms=scoop.received_ms), now_ms, head)

    async def scoop_failed(self, store: Store, scoop: ClaimRow, reason: str, by: SourceRow,
                           now_ms: int, head: Optional[int] = None, firm: bool = False) -> None:
        """A trusted page shows a held scoop got the event wrong. Trusted claims held behind
        it go on as usual. A mismatch costs the scoop's debt."""
        if reason != "scoop_mismatch":
            await self._reject(store, scoop, reason, now_ms)
            return
        row = await self._reject_with_debt(store, scoop, reason, now_ms, await _head(head))
        ev = await store.get_event(scoop.event_id) if scoop.event_id is not None else None
        theirs = await store.get_source(scoop.source_id) if scoop.source_id is not None else None
        if ev is not None and theirs is not None:
            await self._scoop_contradicted(store, ev, scoop, theirs, by, now_ms, head, firm)

    @staticmethod
    async def _scoop_contradicted(store: Store, ev: EventRecord, scoop: ClaimRow,
                                  theirs: SourceRow, by: SourceRow, now_ms: int,
                                  head: Optional[int], firm: bool = False) -> None:
        """A later trusted page disagrees with a scoop. An official page that settles the
        outcome proves it wrong; any other is left to the operator."""
        await trust.contradict(store, ev.id, theirs, scoop, by, now_ms)
        if firm:
            await trust.proved_wrong(store, trust.CONTRADICT, scoop, "official page", "resolver",
                                     now_ms, head, mark_event=False)
        else:
            await trust.log(store, trust.FLAG_CONTRADICTED, None, str(scoop.id),
                            f"contradicted by {trust.label(by)}", "resolver", theirs.id,
                            now_ms=now_ms)

    # Filings

    async def refile(self, store: Store, row: ClaimRow, now_ms: int,
                     head: Optional[int] = None) -> str:
        """A new claim on a filing its subject made, held as a scoop or turned away as low trust
        because the filer was not confirmed when it was checked: placed as the trusted claim it
        is, while its event is still open to it. Returns the outcome."""
        from app.services import verifier

        if (trust.filing_of(row.source_url) is None or row.novelty != novelty.NEW
                or row.route != "scoop"):
            return "not_a_filing_scoop"
        source = await store.get_source(row.source_id) if row.source_id is not None else None
        ev = await store.get_event(row.event_id) if row.event_id is not None else None
        if source is None or ev is None:
            return "not_placed"
        record = ledger.judge_record(await store.source_fetches(row.id))
        if not record or record.get("outcome") != "pass":
            # A trusted claim is paid on a passed page-judge record, which validators check.
            return "no_judge_pass"
        v = verifier.get_verifier()
        await v.registry.refresh(store)
        claim = Claim.model_validate(row.payload)
        ct = await trust.of_claim(store, v.registry, self.index(), claim, source, row.category,
                                  row.source_url)
        if ct.t < trust.PRIZE_TRUST_MIN:
            return "not_filed_by_subject"
        async with ledger.claim_lock(row.id):
            row = await store.get_claim(row.id)
            if row.status == "verified":
                if ev.first_claim_id not in (None, row.id):
                    return "event_taken"
                if any(c.route == "scoop" and c.id != row.id
                       for c in await store.claims_for_event(ev.id, ("verified",))):
                    return "rival_scoops"
                for cand in await store.scoop_candidates(row.id, (scoops.QUEUED,)):
                    await store.update_scoop_candidate(cand.id, status=scoops.CLOSED)
                await store.update_claim(row.id, "verified", route="event", trust=ct.t,
                                         trust_band=ct.band)
                await store.update_event(ev.id, confidence=REPORTED, first_claim_id=row.id)
                await store.update_facts(row.id, "accepted")
                row = await store.get_claim(row.id)
                a = await points.claim_age(store, row, claim.event_date)
                await points.pay(store, row, "event",
                                 points.factors(claim.event_type, "event", ct.t, a,
                                                received_ms=row.received_ms), now_ms, head)
                return "paid"
            if row.status == "rejected" and row.reason == "low_trust":
                if ev.confidence != LINE or ev.first_claim_id is not None:
                    return "event_taken"
                # Back to the resolver, which places it as the head of its line.
                await store.update_claim(row.id, "rejected", status="fetched", reason=None,
                                         trust=ct.t, trust_band=ct.band)
                return "reopened"
        return "not_eligible"

    # Scoop windows

    async def close_windows(self, store: Store, now_ms: int, head: Optional[int] = None) -> None:
        """Close held scoops whose window has ended, and pay verified prize claims whose pay
        failed. Every verified claim is looked at, a page at a time, oldest first; one that
        fails is left for the next pass."""
        after = None
        while True:
            page = await store.claims_by_status("verified", limit=CLOSE_PAGE, after=after)
            for row in page:
                try:
                    head = await self._close(store, row, now_ms, head)
                except intake.InvalidTransition:
                    pass
                except Exception as e:
                    logger.warning(f"Closing or paying claim {row.id} failed: {e!r}")
            if len(page) < CLOSE_PAGE:
                return
            after = (page[-1].bucket, page[-1].id)

    async def _close(self, store: Store, row: ClaimRow, now_ms: int,
                     head: Optional[int]) -> Optional[int]:
        """Pay or close one verified claim when it is due; returns the chain head, once read."""
        if row.route == "event":
            await self.repay(store, row, now_ms, head)
            return head
        end = row.received_ms + int(novelty.SCOOP_WINDOW_H * HOUR_MS)
        if row.route != "scoop" or now_ms < end:
            return head
        # A page found before the end is checked first.
        if await store.scoop_candidates(row.id, ("queued",)):
            return head
        head = await _head(head)
        if await self._confirmed(store, row) is not None:
            await self.repay(store, row, now_ms, head)
            return head
        async with ledger.claim_lock(row.id):
            row = await store.get_claim(row.id)
            if row is None or row.status != "verified":
                return head
            if await self._confirmed(store, row) is not None:
                return head
            if await self._lost_on_timing(store, row):
                await self._reject(store, row, "lost_line", now_ms)
            else:
                await self._reject_with_debt(store, row, "scoop_unconfirmed", now_ms, head)
        return head

    async def repay(self, store: Store, row: ClaimRow, now_ms: int,
                    head: Optional[int] = None) -> Optional[ClaimRow]:
        """Pay a verified claim whose pay did not commit, as it would have been paid: an
        event-route claim that took its event, at its frozen trust and age, once PAY_RETRY_MS
        has passed since it was verified; a scoop whose confirmation was recorded, at that
        confirmation's trust and multiple. None when it is neither. A claim whose paid row
        is already written (by the pay before it was atomic) is moved on without a second
        row. The claim's move to provisional commits with its row and only from verified, so
        two pays of one claim write one row."""
        if row.route == "event" and now_ms - (row.verified_ms or now_ms) < PAY_RETRY_MS:
            return None
        async with ledger.claim_lock(row.id):
            row = await store.get_claim(row.id)
            if row is None or row.status != "verified" or row.route not in PRIZE_ROUTES:
                return None
            paid = [r for r in await store.claim_ledger(row.id) if r.kind == ledger.PROVISIONAL]
            if paid:
                logger.warning(f"Claim {row.id} has its paid row but is still verified; "
                               f"moving it on without a second row")
                return await points.finish_pay(store, row, paid[0], now_ms)
            claim = Claim.model_validate(row.payload)
            if row.route == "scoop":
                confirm = await self._confirmed(store, row)
                if confirm is None:
                    return None
                ev = await store.get_event(row.event_id) if row.event_id is not None else None
                if ev is not None and ev.first_claim_id is None:
                    await store.update_event(ev.id, confidence=REPORTED, first_claim_id=row.id)
                a = await points.claim_age(store, row, claim.event_date)
                f = points.factors(claim.event_type, "scoop", float(confirm["trust"]), a,
                                   points.confirm_multiple(row, confirm),
                                   received_ms=row.received_ms)
                logger.warning(f"Paying confirmed scoop {row.id} whose pay did not commit")
                return await points.pay(store, row, "scoop", f, now_ms, head)
            ev = await store.get_event(row.event_id) if row.event_id is not None else None
            if ev is None or ev.first_claim_id != row.id:
                return None
            t, _ = await self._trust(store, row)
            a = await points.claim_age(store, row, claim.event_date)
            logger.warning(f"Paying claim {row.id} whose pay did not commit")
            return await points.pay(store, row, "event", points.factors(
                claim.event_type, "event", t, a, received_ms=row.received_ms), now_ms, head)

    @staticmethod
    async def _confirmed(store: Store, scoop: ClaimRow) -> Optional[dict]:
        """The confirmation recorded on a held scoop, which is paid with it."""
        return (ledger.judge_record(await store.source_fetches(scoop.id)) or {}).get("confirm")

    async def _lost_on_timing(self, store: Store, scoop: ClaimRow) -> bool:
        """Another claim took the scoop's event with an outcome that does not conflict."""
        ev = await store.get_event(scoop.event_id) if scoop.event_id is not None else None
        if ev is None or ev.first_claim_id in (None, scoop.id):
            return False
        taker = await store.get_claim(ev.first_claim_id)
        if taker is None:
            return False
        ours, theirs = Claim.model_validate(scoop.payload), Claim.model_validate(taker.payload)
        index = self.index()
        return not (novelty.conflicting(ours, theirs, index)
                    or novelty.decided_otherwise(ours, theirs, index))


def checked_fetch(fetches: Sequence[SourceFetchRow]) -> Optional[SourceFetchRow]:
    """The fetch the page check last decided on (it records its date gate), else None."""
    return next((f for f in reversed(fetches) if "date_gate" in (f.check or {})), None)


async def _head(head: Optional[int]) -> int:
    """The chain head a debt row is stamped with, found before the claim closes."""
    head = await block.current_block() if head is None else head
    if head is None:
        raise RuntimeError("chain head unavailable")
    return head


_default: Optional[Resolver] = None


def get_resolver() -> Resolver:
    global _default
    if _default is None:
        _default = Resolver()
    return _default
