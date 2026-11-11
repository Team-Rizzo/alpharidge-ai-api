"""Match answer-key items to known events and check the claims on them field by field."""

from __future__ import annotations

import logging
import math
import re
from typing import Iterable, List, Optional, Sequence

from alpharidge_ai.schema import Claim
from alpharidge_ai.verify import floor
from app.config import CATEGORIES
from app.services import novelty, product, trust
from app.services.answer_key import accuracy
from app.services.answer_key.base import AK_MATCH_H, to_probe
from app.services.entities import EntityIndex, PlaceIndex
from app.services.store import WON, AccuracyCheckRow, AnswerKeyRow, ClaimRow, Store
from app.utils.clock import HOUR_MS

AK_GAP_H = 24
AK_VALUE_TOL = 0.01
AK_ABS_TOL = 0.05

logger = logging.getLogger(__name__)

OPEN, GAP, MATCHED, CONFLICT = "open", "gap", "matched", "conflict"
LIVE = (OPEN, GAP, MATCHED, CONFLICT)
CLAWED = trust.CLAWED[trust.ANSWER_KEY]
# Adapters whose figures can prove a claim wrong.
PROOF_ADAPTERS = ("fed", "bls")

_SCALES = (1.0, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9)
_DASH = re.compile(r"\s*[-–—:]\s*")


def _field(name) -> str:
    return str(name or "").strip().lower().replace(" ", "_")


def _text(v) -> str:
    return _DASH.sub("-", " ".join(str(v).lower().split()))


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _close(a: float, b: float) -> bool:
    return (abs(a - b) <= AK_ABS_TOL
            or abs(a - b) <= AK_VALUE_TOL * max(abs(a), abs(b)))


def agree(ref: dict, got) -> Optional[bool]:
    """Whether a claimed fact agrees with the record's; None when they can't be compared."""
    if ref.get("exact"):
        if not isinstance(got.value, str):
            return None
        return _text(got.value) in {_text(v) for v in (ref["value"], *ref.get("alts", ()))}
    want = [x for x in (_num(v) for v in (ref["value"], *ref.get("alts", ()))) if x is not None]
    have = _num(got.value)
    if not want or have is None:
        return None
    ru, gu = floor.unit_class(ref.get("unit")), floor.unit_class(got.unit)
    if "pct" in (ru, gu):
        if ru != gu:
            return None
        scale = floor.unit_magnitude(got.unit) / floor.unit_magnitude(ref.get("unit"))
        return any(_close(abs(have) * scale, abs(w)) for w in want)
    return any(_close(abs(have) * s, abs(w)) for w in want for s in _SCALES)


def related(payload: dict, facts: Iterable) -> bool:
    """False when the claim names a different subject in one of the record's guard fields."""
    mine = {_field(f.field): f for f in facts}
    refs = {_field(f["field"]): f for f in payload.get("facts", ())}
    for g in payload.get("guards", ()):
        ref, got = refs.get(_field(g)), mine.get(_field(g))
        if ref is not None and got is not None and agree(ref, got) is False:
            return False
    return True


def labelled(ref: dict, mine: dict) -> bool:
    """The claim names one of the record's labels for the figure."""
    for name, values in ref.get("labels", {}).items():
        got = mine.get(_field(name))
        if got is None or _text(got.value) not in {_text(v) for v in values}:
            return False
    return True


def compare(payload: dict, facts: Sequence) -> Optional[bool]:
    """True when the compared fields agree, False on a disagreement, None when none compare."""
    if not related(payload, facts):
        return None
    mine = {_field(f.field): f for f in facts}
    guards = {_field(g) for g in payload.get("guards", ())}
    seen = False
    for ref in payload.get("facts", ()):
        name = _field(ref["field"])
        got = mine.get(name)
        if name in guards or got is None or not labelled(ref, mine):
            continue
        verdict = agree(ref, got)
        if verdict is False:
            return False
        seen = seen or verdict is True
    return True if seen else None


def named_probes(payload: dict, probe: Claim) -> List[Claim]:
    """The probe, then the probe under each other name of its guard fields (BLS gives
    "Consumer Price Index (CPI)", a miner "CPI")."""
    out = [probe]
    guards = {_field(g) for g in payload.get("guards", ())}
    for ref in payload.get("facts", ()):
        name = _field(ref["field"])
        if name not in guards:
            continue
        for alt in ref.get("alts", ()):
            facts = [f.model_copy(update={"value": alt}) if _field(f.field) == name else f
                     for f in probe.facts]
            out.append(probe.model_copy(update={"facts": facts}))
    return out


def _unmatched_status(row: AnswerKeyRow, now_ms: int) -> str:
    if row.category not in CATEGORIES:
        return row.status
    return GAP if now_ms - row.observed_ms >= AK_GAP_H * HOUR_MS else row.status


clawable = trust.clawable


async def check_claim(store: Store, row: AnswerKeyRow, claim_row: ClaimRow, now_ms: int,
                      head: Optional[int] = None) -> Optional[bool]:
    """Record one answer-key check of a claim."""
    claim = Claim.model_validate(claim_row.payload)
    verdict = compare(row.payload, claim.facts)
    if verdict is None:
        if not related(row.payload, claim.facts):
            return None
        verdict = True
    added = await store.add_accuracy_check(AccuracyCheckRow(
        claim_id=claim_row.id, hotkey=claim_row.hotkey, category=claim_row.category,
        kind=accuracy.ANSWER_KEY, correct=verdict, at_ms=now_ms, ref=str(row.id)))
    if added and not verdict and row.adapter in PROOF_ADAPTERS:
        await trust.proved_wrong(store, trust.ANSWER_KEY, claim_row, f"answer key {row.id}",
                                 "answer_key", now_ms, head)
    return verdict


async def match_item(store: Store, row: AnswerKeyRow, entities: EntityIndex,
                     places: PlaceIndex, now_ms: int, head: Optional[int] = None) -> str:
    """Find the item's event, check the claims on it and set the official-record flag."""
    try:
        probe = to_probe(row, entities)
    except ValueError as e:
        logger.warning(f"Answer key: item {row.id} is not a valid probe: {e!r}")
        return row.status
    for p in named_probes(row.payload, probe):
        ev = await novelty.find_known(store, p, entities, places)
        if ev is not None:
            break
    if ev is None:
        status = _unmatched_status(row, now_ms)
        if status != row.status:
            await store.update_answer_item(row.id, status=status)
        return status

    accepted = [f for f in await store.event_facts(ev.id) if f.status == "accepted"]
    conflict = compare(row.payload, accepted) is False
    changed = ev.official_match != (not conflict)
    if changed:
        await store.update_event(ev.id, official_match=not conflict)
    for claim_row in await store.claims_for_event(ev.id, WON):
        changed |= await check_claim(store, row, claim_row, now_ms, head) is False
    if changed:
        await product.sync_events(store, [ev.id], now_ms, entities)

    status = CONFLICT if conflict else MATCHED
    fields = {"status": status, "matched_event_id": ev.id,
              "matched_claim_id": ev.first_claim_id}
    if row.matched_ms is None:
        fields["matched_ms"] = now_ms
    if any(getattr(row, k) != v for k, v in fields.items()):
        await store.update_answer_item(row.id, **fields)
    return status


async def match_open(store: Store, entities: EntityIndex, places: PlaceIndex, now_ms: int,
                     head: Optional[int] = None) -> int:
    """Match every item still inside the match window; checks are recorded once per claim."""
    since = now_ms - int(AK_MATCH_H * HOUR_MS)
    matched = 0
    for row in await store.answer_items(LIVE, observed_since_ms=since):
        try:
            matched += await match_item(store, row, entities, places, now_ms, head) in (
                MATCHED, CONFLICT)
        except Exception as e:
            logger.warning(f"Answer key: match of item {row.id} failed: {e!r}")
    return matched


async def gap_alerts(store: Store, now_ms: int) -> List[dict]:
    """Recent records no miner reported, for operator alerts. Never for public responses."""
    since = now_ms - int(AK_MATCH_H * HOUR_MS)
    return [{"adapter": r.adapter, "category": r.category, "event_type": r.event_type,
             "event_date": r.event_date.isoformat() if r.event_date else None,
             "title": r.payload.get("title"), "observed_ms": r.observed_ms}
            for r in await store.answer_items((GAP,), observed_since_ms=since)]
