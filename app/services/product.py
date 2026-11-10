"""Product view of events: confidence, labels, our own headline, and the append-only feed.

Responses carry facts, our headline and source links only: no hotkeys, trust numbers,
answer-key data or pay.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from alpharidge_ai.schema import Actor, Claim, IdScheme, headline
from app.config import EVENT_LANE
from app.services import ledger, trust
from app.services.entities import LINKED, EntityIndex, EntityRecord, id_key, normalize_name
from app.services.store import (
    PRIZE_ROUTES, WON, ClaimRow, EventFactRow, EventRecord, FeedRow, SourceRow, Store,
)
from app.utils.clock import iso

ALLEGATION_TYPES = ("legal", "regulatory", "tech_regulation")
# Types where an actor not linked to a named non-person record counts as a person.
PERSON_HOLD_TYPES = ("legal", "regulatory", "tech_regulation", "personnel", "appointment")
PERSON_ROLES = ("person", "individual", "executive", "official", "founder", "appointee",
                "candidate", "player", "athlete", "coach", "defendant", "plaintiff", "recipient")
PERSON_KINDS = ("person", "public_person", "executive")
# Person kinds allowed as actors when the record links a public role.
PUBLIC_PERSON_KINDS = ("official", "athlete")

logger = logging.getLogger(__name__)

CONFIRMED, REPORTED, UNVERIFIED, LINE = "confirmed", "reported", "unverified", "line"
PUBLIC = (CONFIRMED, REPORTED)
EVENT, STAGE, FACT, CONFIDENCE, MARKED = "event", "stage", "fact", "confidence", "marked"
# Next to each source_url: when it may be shown. Never served.
OPEN_AT = "_source_open_ms"


def parse_ms(text: str) -> int:
    """RFC 3339 time or date as epoch ms, UTC when no offset is given."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp() * 1000)


def settle_ms() -> int:
    return int(ledger.params().settle_delay_h * 3_600_000)


def open_ms(c: Optional[ClaimRow]) -> Optional[int]:
    """When the claim's source may be shown: once the paid claim settles."""
    return c.settle_ms if c is not None else None


def hide_sources(obj, now_ms: int):
    """`obj` with each source_url whose claim has not settled left out."""
    if isinstance(obj, list):
        return [hide_sources(x, now_ms) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {k: hide_sources(v, now_ms) for k, v in obj.items() if k != OPEN_AT}
    at = obj.get(OPEN_AT)
    if out.get("source_url") and (at is None or at > now_ms):
        out["source_url"] = None
    return out


def hold_ms() -> int:
    return int(EVENT_LANE["PUBLISH_HOLD_S"] * 1000)


def entity_keys(entities: EntityIndex, text: str) -> Tuple[str, ...]:
    """Identity keys for "scheme:id"; ValueError when it is not one."""
    scheme, _, ext = text.partition(":")
    if not ext or scheme not in {s.value for s in IdScheme}:
        raise ValueError(text)
    rec = entities.get(scheme, ext)
    return rec.keys if rec is not None else (id_key(scheme, ext),)


def entity_json(rec: EntityRecord) -> dict:
    return {"id": rec.key, "scheme": rec.scheme, "external_id": rec.external_id,
            "name": rec.name, "aliases": list(rec.aliases)}


# Who and what the event is about

def _link(entities: EntityIndex, actor: Actor) -> Optional[EntityRecord]:
    link = entities.link(actor)
    return link.entity if link is not None else None


def _record(entities: EntityIndex, actor: Actor) -> Optional[EntityRecord]:
    """The record the actor's ID names, linked or not."""
    scheme = IdScheme(actor.id_scheme).value
    return entities.get(scheme, actor.id) if scheme != IdScheme.OTHER.value else None


def _is_person(actor: Actor, rec: Optional[EntityRecord]) -> bool:
    if rec is not None:
        return rec.kind in (*PERSON_KINDS, *PUBLIC_PERSON_KINDS)
    return (actor.role or "").strip().lower() in PERSON_ROLES


def _public_person(rec: Optional[EntityRecord]) -> bool:
    return (rec is not None and rec.status == LINKED and rec.scheme == IdScheme.WIKIDATA.value
            and rec.kind in PUBLIC_PERSON_KINDS and bool(rec.roles))


def _named(actor: Actor, rec: EntityRecord) -> bool:
    name = normalize_name(actor.name or "")
    return not name or name in {normalize_name(n) for n in (rec.name, *rec.aliases)}


def _persons(actors: Iterable[Actor], entities: EntityIndex,
             strict: bool = False) -> List[Optional[EntityRecord]]:
    """Linked records (None when unlinked or named differently) of the actors that are people.
    When strict, an actor counts as a person unless it links to a named non-person record."""
    out: List[Optional[EntityRecord]] = []
    for a in actors:
        linked = _link(entities, a)
        rec = linked
        if rec is None:
            rec = _record(entities, a)
            rec = rec if rec is not None and _is_person(a, rec) else None
        if _is_person(a, rec):
            out.append(rec if rec is not None and _named(a, rec) else None)
        elif strict and (linked is None or not _named(a, linked)):
            out.append(None)
    return out


def labelled(event_type: str, actors: Iterable[Actor], entities: EntityIndex) -> bool:
    """About a person who is not a linked public figure, or an allegation about a person:
    served as unverified until confirmed."""
    persons = _persons(actors, entities, event_type in PERSON_HOLD_TYPES)
    if any(not _public_person(rec) for rec in persons):
        return True
    return bool(persons) and event_type in ALLEGATION_TYPES


def labelled_claim(claim: Claim, entities: EntityIndex) -> bool:
    return labelled(claim.event_type.value, claim.actors, entities)


@dataclass
class View:
    lead: Optional[ClaimRow]
    actors: List[Actor] = field(default_factory=list)
    claims: Dict[int, ClaimRow] = field(default_factory=dict)
    trust: Dict[int, float] = field(default_factory=dict)
    owners: set = field(default_factory=set)
    facts: List[EventFactRow] = field(default_factory=list)


async def _lead(store: Store, ev: EventRecord,
                claims: Iterable[ClaimRow]) -> Optional[ClaimRow]:
    if ev.first_claim_id is not None:
        return await store.get_claim(ev.first_claim_id)
    leads = sorted((c for c in claims if c.route in PRIZE_ROUTES), key=lambda c: c.id)
    return leads[0] if leads else None


async def view(store: Store, ev: EventRecord) -> View:
    claims = {c.id: c for c in await store.claims_for_event(ev.id, WON)}
    lead = await _lead(store, ev, claims.values())
    v = View(lead=lead, claims=claims)
    if lead is not None:
        v.actors = list(Claim.model_validate(lead.payload).actors)
    trusted = trust.PRIZE_TRUST_MIN
    facts = await store.event_facts(ev.id)
    for c in claims.values():
        source = await store.get_source(c.source_id) if c.source_id is not None else None
        if source is None:
            continue
        v.trust[c.id] = c.trust or 0.0
        if v.trust[c.id] >= trusted:
            v.owners.add(owner_key(source))
    for se in await store.source_events(event_id=ev.id):
        if se.role not in (trust.FIRST, trust.CONFIRM) or se.claim_id is None:
            continue
        if se.role == trust.CONFIRM:
            # Written only for a trusted page that passed the check.
            source = await store.get_source(se.source_id)
            if source is not None and source.status not in trust.BLOCKED:
                v.owners.add(se.owner_id or f"source:{se.source_id}")
            continue
        c = claims.get(se.claim_id) or await store.get_claim(se.claim_id)
        if c is not None and (c.trust or 0.0) >= trusted:
            v.owners.add(se.owner_id or f"source:{se.source_id}")
    v.facts = [f for f in facts if f.status != "offschema"
               and (f.claim_id is None or f.claim_id in claims)]
    return v


def owner_key(source: SourceRow) -> str:
    return source.owner_id or f"source:{source.id}"


async def counted_owners(store: Store, ev: EventRecord) -> set:
    """The owners that count toward `confirmed`, and the owner of the event's first claim."""
    owners = set((await view(store, ev)).owners)
    lead = await store.get_claim(ev.first_claim_id) if ev.first_claim_id else None
    source = await store.get_source(lead.source_id) if lead and lead.source_id else None
    if source is not None:
        owners.add(owner_key(source))
    return owners


def confidence_of(ev: EventRecord, v: View) -> str:
    if ev.first_claim_id is None:
        return UNVERIFIED
    if ev.official_match or len(v.owners) >= 2:
        return CONFIRMED
    return REPORTED


# Response shapes

def _headline(ev: EventRecord, actors: List[Actor], entities: EntityIndex) -> str:
    names = []
    for a in actors:
        rec = _link(entities, a)
        names.append(rec.name if rec is not None else (a.name or a.id))
    return headline(ev.event_type, names, ev.stage, ev.place_key or None)


def _summary(ev: EventRecord, lead: Optional[ClaimRow], actors: List[Actor],
             entities: EntityIndex) -> dict:
    return {"id": str(ev.id), "headline": _headline(ev, actors, entities),
            "category": ev.category, "type": ev.event_type, "stage": ev.stage,
            "event_date": ev.event_date.isoformat(), "first_seen_at": iso(ev.first_seen_ms),
            "confidence": ev.confidence, "marked": ev.marked,
            "source_url": lead.source_url if lead else None, OPEN_AT: open_ms(lead)}


async def summary(store: Store, ev: EventRecord, entities: EntityIndex) -> dict:
    scoops = [] if ev.first_claim_id is not None else await store.claims_for_event(
        ev.id, WON)
    lead = await _lead(store, ev, scoops)
    actors = list(Claim.model_validate(lead.payload).actors) if lead else []
    return _summary(ev, lead, actors, entities)


async def _claim(store: Store, v: View, claim_id: Optional[int]) -> Optional[ClaimRow]:
    if claim_id is None:
        return None
    if claim_id not in v.claims:
        row = await store.get_claim(claim_id)
        if row is None:
            return None
        v.claims[claim_id] = row
    return v.claims[claim_id]


async def _stages(store: Store, ev: EventRecord, v: View) -> List[dict]:
    out = []
    for stage, claim_id, at_ms in await store.event_stages(ev.id):
        c = await _claim(store, v, claim_id)
        out.append({"stage": stage, "at": iso(at_ms), "source_url": c.source_url if c else None,
                    OPEN_AT: open_ms(c)})
    return out


def _fact(f: EventFactRow, v: View) -> dict:
    c = v.claims.get(f.claim_id)
    return {"id": str(f.id), "slot": f.slot or f.field, "value": f.value, "unit": f.unit,
            "source_url": f.source_url, "first_seen_at": iso(c.received_ms if c else None),
            "status": f.status, OPEN_AT: open_ms(c)}


def _actor(a: Actor, entities: EntityIndex) -> dict:
    rec = _link(entities, a)
    if rec is None:
        return {"id": None, "scheme": a.id_scheme.value, "external_id": a.id,
                "name": a.name or a.id, "role": a.role}
    return {"id": rec.key, "scheme": rec.scheme, "external_id": rec.external_id,
            "name": rec.name, "role": a.role}


async def detail(store: Store, ev: EventRecord, entities: EntityIndex) -> dict:
    v = await view(store, ev)
    iso2, _, admin1 = (ev.place_key or "").partition("-")
    return {
        **_summary(ev, v.lead, v.actors, entities),
        "stages": await _stages(store, ev, v),
        "facts": [_fact(f, v) for f in v.facts],
        "entities": [_actor(a, entities) for a in v.actors],
        "place": {"iso2": iso2, "admin1": admin1 or None} if iso2 else None,
        "official_match": ev.official_match,
    }


def visible(ev: Optional[EventRecord], now_ms: int, include_unverified: bool = False) -> bool:
    return (ev is not None and ev.confidence != LINE and ev.hold is None
            and ev.publish_ms is not None and ev.publish_ms <= now_ms
            and (include_unverified or ev.confidence != UNVERIFIED))


async def labelled_summary(store: Store, ev: Optional[EventRecord], now_ms: int,
                           entities: EntityIndex) -> dict:
    """Headline and verification label of a published event, unverified included."""
    if not visible(ev, now_ms, include_unverified=True):
        return {"headline": None, "verification": None}
    s = await summary(store, ev, entities)
    return {"headline": s["headline"], "verification": s["confidence"]}


def record_json(r: FeedRow) -> dict:
    return {"seq": str(r.seq), "at": iso(r.at_ms), "kind": r.kind, "event_id": str(r.event_id),
            **r.payload}


# Sync after every attach

async def _append(store: Store, ev: EventRecord, v: View, now_ms: int,
                  entities: EntityIndex) -> List[FeedRow]:
    stages = await _stages(store, ev, v)
    facts = [_fact(f, v) for f in v.facts]
    seen_stages, seen_facts, last, started = set(), set(), None, False
    marked = None
    for r in await store.feed(event_id=ev.id, limit=1_000_000):
        if r.kind == EVENT:
            started, last = True, r.payload.get("confidence")
            marked = r.payload.get("marked")
            seen_stages |= {s["stage"] for s in r.payload.get("stages", ())}
            seen_facts |= {f["id"] for f in r.payload.get("facts", ())}
        elif r.kind == STAGE:
            seen_stages.add(r.payload.get("stage"))
        elif r.kind == FACT:
            seen_facts.add(r.payload.get("id"))
        elif r.kind == CONFIDENCE:
            last = r.payload.get("confidence")
        elif r.kind == MARKED:
            marked = r.payload.get("marked")

    def row(kind: str, payload: dict) -> FeedRow:
        return FeedRow(kind=kind, event_id=ev.id, category=ev.category,
                       event_type=ev.event_type, at_ms=now_ms, payload=payload)

    if not started:
        body = {**_summary(ev, v.lead, v.actors, entities), "stages": stages, "facts": facts}
        body.pop("id")
        out = [row(EVENT, body)]
    else:
        out = [row(STAGE, s) for s in stages if s["stage"] not in seen_stages]
        out += [row(FACT, f) for f in facts if f["id"] not in seen_facts]
        if ev.confidence != last:
            out.append(row(CONFIDENCE, {"confidence": ev.confidence, "previous": last}))
        if ev.marked != marked:
            out.append(row(MARKED, {"marked": ev.marked}))
    return [await store.add_feed(r) for r in out]


async def sync_event(store: Store, event_id: int, now_ms: int,
                     entities: EntityIndex) -> List[FeedRow]:
    """Recompute confidence, set the publish time, and append feed records. A labelled event
    stays unverified until it is confirmed."""
    ev = await store.get_event(event_id)
    if ev is None or ev.confidence == LINE:
        return []
    v = await view(store, ev)
    confidence = confidence_of(ev, v)
    if confidence != CONFIRMED and labelled(ev.event_type, v.actors, entities):
        confidence = UNVERIFIED
    fields = {"confidence": confidence, "hold": None}
    if ev.first_seen_ms is None and v.lead is not None:
        fields["first_seen_ms"] = v.lead.received_ms
    if fields["hold"] is None and ev.publish_ms is None:
        fields["publish_ms"] = now_ms + hold_ms()
    fields = {k: val for k, val in fields.items() if getattr(ev, k) != val}
    if fields:
        await store.update_event(ev.id, **fields)
        ev = replace(ev, **fields)
    if ev.hold is not None or ev.confidence == UNVERIFIED:
        return []
    return await _append(store, ev, v, now_ms, entities)


async def sync_events(store: Store, event_ids: Iterable[int], now_ms: int,
                      entities: EntityIndex) -> int:
    added = 0
    for event_id in sorted(set(event_ids)):
        try:
            added += len(await sync_event(store, event_id, now_ms, entities))
        except Exception as e:
            logger.warning(f"Product sync of event {event_id} failed: {e!r}")
    return added
