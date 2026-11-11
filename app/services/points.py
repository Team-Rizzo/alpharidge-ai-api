"""Points for verified claims: the factors, the 40/60 split and settle at +72 h.

    points = tier price × T × A
    a confirmed scoop's points = tier price × M × T × A, M = SCOOP_PREMIUM when the confirming
    page was published after the scoop's receipt, else 1

The type's tier sets the price, T is the trust frozen at verification and A the age factor at
receipt of the claimed event date or the page's publish date, the earlier. The miner's
category label is never an input.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from alpharidge_ai.schema import TYPE_SPEC, Claim, EventType, category_of, judged
from alpharidge_ai.schema.types import (SCOOP_PREMIUM, TIER_PRICE, age_factor, scoop_debt,
                                        scoop_multiple)
from alpharidge_ai.verify.check import verdict_hash
from alpharidge_ai.verify.dates import parse_datetime
from app.services import recheck, intake, ledger, trust
from app.services.store import PASSED, ClaimRow, LedgerRow, Store
from app.utils import block, hotkey_whitelist
from app.utils.clock import HOUR_MS


@dataclass(frozen=True)
class Factors:
    base: float
    T: float
    A: float = 1.0

    @property
    def product(self) -> float:
        return self.base * self.T * self.A

    def as_dict(self) -> dict:
        return {k: round(v, 6) for k, v in asdict(self).items()}


def price(event_type) -> float:
    return TIER_PRICE.get(TYPE_SPEC[EventType(event_type)].tier, 0)


def base_points(route: str, event_type, multiple: float = SCOOP_PREMIUM) -> float:
    if route == "scoop":
        return price(event_type) * multiple
    return price(event_type) if route == "event" else 0.0


def confirm_multiple(scoop: ClaimRow, confirm: dict) -> float:
    """The scoop's price multiple from its confirm record's publish time and contested list,
    as validators recompute it."""
    return scoop_multiple(parse_datetime(confirm.get("published_at")), _utc(scoop.received_ms),
                          contested=bool(confirm.get("contested")))


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def age(event_date, received_ms: int, published_ms: Optional[int] = None) -> float:
    return age_factor(event_date, _utc(received_ms),
                      _utc(published_ms) if published_ms is not None else None)


async def claim_age(store: Store, row: ClaimRow, event_date) -> float:
    """The age factor from the event date or the judged page's publish date, the earlier, as
    validators recompute it from the sample; the event date alone for an unread page."""
    fetches = await store.source_fetches(row.id)
    return age(event_date, row.received_ms, ledger.published_ms(fetches))


def factors(event_type, route: str, t: float, a: float,
            multiple: float = SCOOP_PREMIUM) -> Factors:
    return Factors(base=base_points(route, event_type, multiple), T=t, A=round(a, 6))


def provisional(f: Factors) -> float:
    return ledger.PROVISIONAL_SHARE * f.product


def settled(f: Factors) -> float:
    return (1.0 - ledger.PROVISIONAL_SHARE) * f.product


# Writing pay

async def debt(store: Store, row: ClaimRow, head: Optional[int] = None) -> float:
    """A missed scoop's debt row, once per claim and never on a paid one. Validators rebuild
    it from the signed body, its judge record and the page's publish time."""
    if row.route != "scoop" or any(r.kind in (ledger.DEBT, ledger.PROVISIONAL)
                                   for r in await store.claim_ledger(row.id)):
        return 0.0
    fetches = await store.source_fetches(row.id)
    record = ledger.judge_record(fetches)
    body = Claim.model_validate_json(row.body)
    claim = judged(body, record or {}) or body
    at = ledger.published_ms(fetches)
    published = _utc(at) if at is not None else None
    pts = scoop_debt(claim.event_type.value, claim.event_date, _utc(row.received_ms), published)
    if pts < 0:
        await ledger.write(store, LedgerRow(
            hotkey=row.hotkey, claim_id=row.id, event_id=row.event_id, kind=ledger.DEBT,
            points=pts, epoch=0, route="scoop", category=category_of(claim.event_type).value,
            reason=row.reason, factors={"verdict": verdict_hash(record)} if record else {}),
            head)
    return pts


async def pay(store: Store, row: ClaimRow, route: str, f: Factors, now_ms: int,
              head: Optional[int] = None) -> ClaimRow:
    """verified -> provisional, writing the 40% row when it is above zero. The row carries
    the hash of the claim's page-judge record, which its signed leaf covers."""
    pts = provisional(f)
    if pts > 0:
        record = ledger.judge_record(await store.source_fetches(row.id))
        signed = {"verdict": verdict_hash(record)} if record else {}
        await ledger.write(store, LedgerRow(
            hotkey=row.hotkey, claim_id=row.id, event_id=row.event_id,
            kind=ledger.PROVISIONAL, points=pts, epoch=0, route=route, category=row.category,
            factors={**f.as_dict(), **signed}), head)
        await recheck.schedule(store, row, route, f.T, now_ms)
    return await intake.transition(store, row, "provisional",
                                   settle_ms=now_ms + int(ledger.params().settle_delay_h * HOUR_MS))


async def coverage_owners(store: Store, row: ClaimRow) -> int:
    """Independent trusted owners with a paid claim on the claim's event, other than the
    winner's."""
    if row.event_id is None:
        return 0
    source = await store.get_source(row.source_id) if row.source_id is not None else None
    winner = source.owner_id if source is not None else None
    coldkey = hotkey_whitelist.coldkey_of(row.hotkey)
    independent: Dict[str, bool] = {}
    for se in await store.source_events(event_id=row.event_id):
        if se.role not in (trust.FIRST, trust.CONFIRM) or not se.owner_id or se.owner_id == winner:
            continue
        via = await store.get_claim(se.claim_id) if se.claim_id is not None else None
        if via is None or via.status not in PASSED or via.event_id != row.event_id:
            continue
        src = await store.get_source(se.source_id)
        if (src is None or (via.trust or 0.0) < trust.PRIZE_TRUST_MIN
                or not (via.trust_band == "primary"
                        or await trust.counts_as_owner(store, src, row.category))):
            continue
        own = hotkey_whitelist.coldkey_of(via.hotkey) == coldkey
        independent[se.owner_id] = independent.get(se.owner_id, False) or not own
    return sum(independent.values())


async def settle_one(store: Store, row: ClaimRow, head: Optional[int] = None) -> ClaimRow:
    source = await store.get_source(row.source_id) if row.source_id is not None else None
    if source is not None and source.status == trust.BANNED:
        await ledger.clawback(store, [row], trust.CLAWED[trust.BANNED], head)
        return await store.get_claim(row.id)
    async with ledger.claim_lock(row.id):
        row = await store.get_claim(row.id) or row
        prov = [r for r in await store.claim_ledger(row.id) if r.kind == ledger.PROVISIONAL]
        if prov and head is None:
            head = await block.current_block()
            if head is None:
                raise RuntimeError("chain head unavailable")
        row = await intake.transition(store, row, "settled")
        for p in prov:
            f = Factors(base=p.factors["base"], T=p.factors["T"], A=p.factors.get("A", 1.0))
            pts = settled(f)
            if pts > 0:
                await ledger.write(store, LedgerRow(
                    hotkey=row.hotkey, claim_id=row.id, event_id=row.event_id,
                    kind=ledger.SETTLE, points=pts, epoch=0, route=p.route,
                    category=p.category, factors=f.as_dict()), head)
    return row


async def settle_due(store: Store, now_ms: int, head: Optional[int] = None,
                     limit: int = 200) -> int:
    """Settle provisional claims whose window has closed; held claims wait."""
    done = 0
    for row in await store.claims_to_settle(now_ms, limit):
        try:
            await settle_one(store, row, head)
            done += 1
        except intake.InvalidTransition:
            pass
    return done


async def resolve_held(store: Store, row: ClaimRow, action: str,
                       head: Optional[int] = None) -> ClaimRow:
    """Operator decision on a held claim: claw it back, or clear the hold."""
    if action == "claw":
        await ledger.clawback(store, [row], "clawed:reverify", head)
    elif action == "clear":
        await store.update_claim(row.id, row.status, held=False)
    else:
        raise ValueError(action)
    return await store.get_claim(row.id)

