"""Novelty: is a claim a new event or a known one? Anything unclear counts as known.

Identity is type group, the type's main subject, coarse place and a date window. Amounts and
other details are facts and never split an event.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from alpharidge_ai.schema import (
    TYPE_SPEC, Actor, Claim, EventType, Fact, IdScheme, has_required, headline,
)
from alpharidge_ai.schema.types import TYPES_PATH
from alpharidge_ai.verify.facts import claims_match
from alpharidge_ai.verify.check import FUTURE_SLACK_D, MAX_AGE_D, PIVOT_SLOTS
from alpharidge_ai.verify.fetch import clean_url
from app.services import intake
from app.services.answer_key.base import indicator_code
from app.services.entities import (
    EntityIndex, PlaceIndex, canonical_id, id_key, normalize_name, places_match,
)
from app.services.store import ClaimRow, DuplicateEvent, EventRecord, Store
from app.services.trust import PRIZE_TRUST_MIN
from app.utils.clock import HOUR_MS

SCOOP_WINDOW_H = 72
# Days claims of one event may date it apart, besides the future slack.
DATE_DRIFT_D = 3
# The slot ("stage" for the end stage) a confirmation settles.
DECISIVE_SLOTS = {"transfer": "to_team", "appointment": "appointee",
                  "merger_acquisition": "stage", "ipo_listing": "exchange",
                  "legislation_passed": "stage", "sanction_or_tariff": "stage"}
# What else two claims must share to be one event, and to decide the same thing, when both
# give it in a firm form (a bill's number, a country, a linked entity); names alone never
# split an event, since one bill or target goes by several.
SAME_SLOTS = {"legislation_passed": "bill", "sanction_or_tariff": "targets", "vulnerability": "cve_id"}
_BILL_NUMBER = re.compile(r"(?:bill )?([a-z]{1,8}) ?(\d{1,5})")
_CVE = re.compile(r"cve ?(\d{4}) ?(\d{4,7})")
_COUNTRIES = {normalize_name(form): c["iso2"] for c in json.loads(
    TYPES_PATH.with_name("countries.json").read_text(encoding="utf-8"))["countries"]
    for form in (c["name"], *c.get("aliases", ()))}
# The other party, which scoops on one subject may also name differently.
COUNTERPARTY_SLOTS = {"merger_acquisition": "acquirer"}
# A second slot that also keys the subject, so two claims whose subject goes by another name
# still meet: the person named to a post, the award a recipient won.
ALSO_SUBJECT = {"appointment": "appointee", "award": "award"}
# Types that fill one post with one person, as (person slot, post slot).
PEOPLE_SLOTS = {"personnel": ("person", "role"), "appointment": ("appointee", "office")}
LEAVING = ("departing", "departed", "left", "leaving", "resigned", "resigning", "resigns",
           "removed", "dismissed", "fired", "ousted", "steps down", "stepped down",
           "stepping down")
LISTING_PRIZES_MAX = 3
# Type groups that can describe the same event.
RELATED_GROUPS = (
    ("deal", "partnership"), ("funding", "ipo_listing"), ("results", "guidance"),
    ("legal", "regulatory"), ("regulatory", "trade_measure"), ("legislation", "trade_measure"),
    ("personnel", "appointment"), ("security_incident", "outage"),
    ("security_incident", "vulnerability"), ("product_launch", "breakthrough"))
# Same document: share of the body trimmed at each end, MinHash size and Jaccard bar.
DOC_TRIM = 0.1
DOC_MINHASH_K = 64
DOC_SAME_JACCARD = 0.6
WIRE_CREDITS = ("ap", "associated press", "reuters", "afp", "bloomberg", "dpa", "upi")
DOC_LEAD_MIN_WORDS = 8


KNOWN, NEW = "known", "new"


@dataclass
class Novelty:
    state: str
    route: Optional[str] = None
    event_id: Optional[int] = None
    reason: Optional[str] = None
    # A new claim that joins an open line waits behind its head.
    line: bool = False
    candidates: List[int] = field(default_factory=list)


@dataclass(frozen=True)
class Identity:
    event_type: EventType
    category: str
    group: str
    actor_keys: frozenset
    actor_ids: Tuple[str, ...]
    names: Tuple[str, ...]
    subject: frozenset
    place_key: Optional[str]
    fingerprint: str
    hard_key: Optional[str]
    # Subject CIKs the claim gives but nothing verified: they find events, never key them.
    claimed: frozenset = frozenset()


def compute_event_fingerprint(event_type: str, event_date: date, window_days: int,
                              actor_ids: Iterable[str], place_key: Optional[str]) -> str:
    """type | date bucket | sorted actor ids | place."""
    actors = ",".join(sorted({a.strip().lower() for a in actor_ids if a and a.strip()}))
    bucket = event_date.toordinal() // max(1, window_days)
    raw = f"{event_type}|{bucket}|{actors}|{place_key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def hard_key(hard_id: Optional[str]) -> Optional[str]:
    if not hard_id or ":" not in hard_id:
        return None
    scheme, value = hard_id.split(":", 1)
    scheme, value = scheme.strip().lower(), value.strip()
    return f"{scheme}:{value}" if scheme and value else None


def identity(claim: Claim, entities: EntityIndex, places: PlaceIndex,
             filer: Optional[str] = None) -> Identity:
    """`filer`: the CIK the SEC filer check verified for this claim."""
    spec = TYPE_SPEC[claim.event_type]
    keys: Set[str] = set()
    ids: List[str] = []
    found: List[str] = []
    verified: Set[str] = set()
    claimed: Set[str] = set()
    roles = _subject_roles(claim.event_type)
    for a in claim.actors:
        keys |= entities.actor_keys(a, claim.event_type)
        link = entities.link(a, claim.event_type)
        found.append(a.name or a.id)
        if link is not None:
            ids.append(link.entity.key)
            found += [link.entity.name, *link.entity.aliases]
        else:
            ids.append(f"name:{normalize_name(a.name or a.id)}")
        keys.add(ids[-1])
        if a.id_scheme == IdScheme.CIK and a.id.strip().isdigit():
            cik = id_key(IdScheme.CIK.value, a.id)
            if filer is not None and canonical_id(IdScheme.CIK.value, a.id) == filer:
                keys.add(cik)
                if a.role in roles:
                    verified.add(cik)
            elif a.role in roles:
                claimed.add(cik)
    subject = subject_keys(claim, entities) | verified
    keys |= subject
    same = SAME_SLOTS.get(claim.event_type.value)
    if same:
        keys |= {f"{SAME}{k}" for k in same_keys(claim, same, entities)}
    claimed -= keys
    names = tuple(dict.fromkeys(n for n in found if n))
    place_key = places.place_key(claim.place, spec.place_level)
    pivot = PIVOT_SLOTS.get(claim.event_type.value)
    slots = [f"slot:{_slot_value(pivot, f.value)}" for f in claim.facts
             if f.field == pivot and isinstance(f.value, str)]
    slots += [f"{SAME}{normalize_name(f.value)}" for f in claim.facts
              if same and f.field == same and isinstance(f.value, str)]
    fp = compute_event_fingerprint(claim.event_type.value, claim.event_date, spec.window_days,
                                   [*ids, *slots], place_key)
    return Identity(claim.event_type, spec.category.value, spec.group, frozenset(keys),
                    tuple(ids), names, frozenset(subject or keys), place_key, fp,
                    hard_key(claim.hard_id), frozenset(claimed))


def _subject_roles(event_type: EventType) -> Set[str]:
    pivot = PIVOT_SLOTS.get(event_type.value)
    return {pivot} if pivot else set(TYPE_SPEC[event_type].subject_roles)


def subject_keys(claim: Claim, entities: EntityIndex) -> Set[str]:
    """Keys of the type's main subject: its pivot slot, else the actors in the type's roles;
    and its ALSO_SUBJECT slot."""
    pivot = PIVOT_SLOTS.get(claim.event_type.value)
    also = ALSO_SUBJECT.get(claim.event_type.value)
    keys = slot_keys(claim, also, entities) if also else set()
    if pivot:
        return keys | slot_keys(claim, pivot, entities)
    roles = set(TYPE_SPEC[claim.event_type].subject_roles)
    for a in claim.actors:
        if a.role in roles:
            keys |= entities.actor_keys(a, claim.event_type)
    return keys


def subject_names(claim: Claim, entities: EntityIndex) -> List[str]:
    """Names the claim gives its main subject, with linked aliases."""
    pivot = PIVOT_SLOTS.get(claim.event_type.value)
    roles = _subject_roles(claim.event_type)
    names = [f.value for f in claim.facts if f.field == pivot and isinstance(f.value, str)]
    for a in claim.actors:
        if a.role in roles:
            names.append(a.name or a.id)
            link = entities.link(a, claim.event_type)
            if link is not None:
                names += [link.entity.name, *link.entity.aliases]
    return [n for n in dict.fromkeys(names) if n]


def _slot_value(slot: str, value: str) -> str:
    code = indicator_code(value) if slot == "indicator" else None
    return f"indicator:{code}" if code else normalize_name(value)


def slot_keys(claim: Claim, slot: str, entities: EntityIndex) -> Set[str]:
    """Identity keys of what a claim names for a slot: string facts and actors in that role.
    A known economic indicator also keys by its code."""
    keys: Set[str] = set()
    for f in claim.facts:
        if f.field == slot and isinstance(f.value, str):
            value = f.value.strip()
            keys |= entities.actor_keys(Actor(id_scheme=IdScheme.OTHER, id=value[:64], name=value),
                                        claim.event_type)
            if slot == "indicator" and indicator_code(value):
                keys.add(_slot_value(slot, value))
    for a in claim.actors:
        if a.role == slot:
            keys |= entities.actor_keys(a, claim.event_type)
    return keys


def same_keys(claim: Claim, slot: str, entities: EntityIndex) -> Set[str]:
    """Keys of a SAME_SLOTS value: its slot keys, a bill's number, a CVE ID and a target
    country."""
    keys = slot_keys(claim, slot, entities)
    for f in claim.facts:
        if f.field == slot and isinstance(f.value, str):
            v = normalize_name(f.value)
            m = _BILL_NUMBER.fullmatch(v) if slot == "bill" else None
            cve = _CVE.fullmatch(v) if slot == "cve_id" else None
            if m:
                keys.add(f"bill:{m.group(1)}{m.group(2)}")
            elif cve:
                keys.add(f"cve:{cve.group(1)}-{int(cve.group(2))}")
            elif slot == "targets" and v in _COUNTRIES:
                keys.add(f"country:{_COUNTRIES[v]}")
    return keys


def _firm(keys: Iterable[str]) -> Set[str]:
    return {k for k in keys if not k.startswith("name:")}


def _differ(ours: Iterable[str], theirs: Iterable[str]) -> bool:
    """Both give a firm form, and none is shared."""
    ours, theirs = _firm(ours), _firm(theirs)
    return bool(ours and theirs) and not ours & theirs


def decided_otherwise(a: Claim, b: Claim, entities: EntityIndex) -> bool:
    """Both claims fill the type's decisive slot, with different values."""
    slot = DECISIVE_SLOTS.get(a.event_type.value)
    if not slot or b.event_type is not a.event_type:
        return False
    same = SAME_SLOTS.get(a.event_type.value)
    if same and _differ(same_keys(a, same, entities), same_keys(b, same, entities)):
        return False
    if slot == "stage":
        spec = TYPE_SPEC[a.event_type]
        ends = {*spec.stages[-1:], *spec.exits}
        return a.stage in ends and b.stage in ends and a.stage != b.stage
    ours, theirs = slot_keys(a, slot, entities), slot_keys(b, slot, entities)
    return bool(ours and theirs) and not ours & theirs


def _hire(c: Claim, entities: EntityIndex) -> Optional[Tuple[Set[str], List[str], Set[str]]]:
    """For a claim that fills a post: who fills it (names), the post's forms, and who is
    named to it (keys); None for a departure."""
    person, post = PEOPLE_SLOTS[c.event_type.value]
    values = {f.field: str(f.value).strip() for f in c.facts if isinstance(f.value, str)}
    move = f" {normalize_name(values.get('direction') or values.get('action') or '')} "
    if any(f" {w} " in move for w in LEAVING):
        return None
    if c.event_type.value == "personnel":
        role = values.get(post, "").lower()
        label = TYPE_SPEC[c.event_type].fields.get(post, {}).get("values", {}).get(role, "")
        label = label.split("(")[0].strip()
        forms = [f for f in {role, label, label.removesuffix(" officer")} if f]
        by = subject_names(c, entities)
    else:
        forms = [normalize_name(values.get(post, ""))]
        by = [values.get("appointed_by", ""), values.get(post, "")]
    names = {normalize_name(n) for n in by if n}
    return names - {""}, [normalize_name(f) for f in forms if f], slot_keys(c, person, entities)


def _hires_differ(a: Claim, b: Claim, entities: EntityIndex) -> bool:
    """Two claims, of either people type, that name different people to one post."""
    ha, hb = _hire(a, entities), _hire(b, entities)
    if ha is None or hb is None or not (ha[2] and hb[2]) or ha[2] & hb[2]:
        return False
    if a.event_type is b.event_type:
        return (bool(subject_keys(a, entities) & subject_keys(b, entities))
                and bool(set(ha[1]) & set(hb[1])))
    (by, forms, _), (other, _, _) = (ha, hb) if a.event_type.value == "personnel" else (
        hb, ha)
    text = " ".join(f" {t} " for t in other)
    return any(_has(text, n) for n in by) and any(_has(text, f) for f in forms)


def conflicting(a: Claim, b: Claim, entities: EntityIndex) -> bool:
    """Same type (personnel and appointment count as one) and subject, and another outcome:
    the decisive slot, the other party, or another person for the post."""
    if a.event_type.value in PEOPLE_SLOTS and b.event_type.value in PEOPLE_SLOTS:
        return _hires_differ(a, b, entities)
    if b.event_type is not a.event_type or not subject_keys(a, entities) & subject_keys(
            b, entities):
        return False
    if decided_otherwise(a, b, entities):
        return True
    slot = COUNTERPARTY_SLOTS.get(a.event_type.value)
    ours, theirs = (slot_keys(a, slot, entities), slot_keys(b, slot, entities)) if slot else (
        set(), set())
    return bool(ours and theirs) and not ours & theirs


SAME = "same:"


def _apart(ident: Identity, cand: EventRecord) -> bool:
    """Both give the type's SAME_SLOTS value in a firm form, and none is shared."""
    return _differ(*({k[len(SAME):] for k in keys if k.startswith(SAME)}
                     for keys in (ident.actor_keys, cand.actor_keys)))


def _group_ok(ident: Identity, spec) -> bool:
    pair = (ident.group, spec.group)
    return spec.group == ident.group or pair in RELATED_GROUPS or pair[::-1] in RELATED_GROUPS


def _overlap(ident: Identity, cand: EventRecord) -> bool:
    return bool(ident.actor_keys & set(cand.actor_keys))


def _matches(ident: Identity, cand: EventRecord, subject: Optional[frozenset] = None) -> bool:
    """Same type group, same main subject and a matching place."""
    spec = TYPE_SPEC.get(_type(cand.event_type))
    if spec is None:
        return False
    subject = ident.subject if subject is None else subject
    return (_group_ok(ident, spec) and bool(subject & set(cand.subject_keys))
            and places_match(ident.place_key, cand.place_key) and not _apart(ident, cand))


def numbers(claim: Claim) -> Tuple[Tuple[str, float, Optional[str]], ...]:
    return tuple((f.field, f.value, f.unit) for f in claim.facts if not isinstance(f.value, str))


def _linked(keys: Iterable[str]) -> bool:
    return any(not k.startswith("name:") for k in keys)


def _same_numbers(ident: Identity, claim: Claim, cand: EventRecord) -> bool:
    """Same type and date, and every figure both give agrees. Subjects that both carry an
    ID are left to the identity rules. A day off still matches, except for types whose
    window is one day (rate decisions, data prints), where distinct releases sit a day apart."""
    slack = 0 if TYPE_SPEC[claim.event_type].window_days <= 1 else 1
    if (cand.event_type != claim.event_type.value or _apart(ident, cand)
            or abs((cand.event_date - claim.event_date).days) > slack
            or (_linked(ident.subject) and _linked(cand.subject_keys))):
        return False
    mine = {f: Fact(field=f, value=v, unit=u) for f, v, u in numbers(claim)}
    theirs = {f: Fact(field=f, value=v, unit=u) for f, v, u in cand.numbers}
    common = mine.keys() & theirs.keys()
    return bool(common) and all(claims_match(mine[f], theirs[f]) for f in common)


def _window(claim: Claim) -> timedelta:
    drift = DATE_DRIFT_D + FUTURE_SLACK_D
    return timedelta(days=TYPE_SPEC[claim.event_type].window_days + drift)


def _attached(ident: Identity, claim: Claim, ev: EventRecord) -> bool:
    """An event a claim names by ID: same group, a shared actor, inside the window."""
    spec = TYPE_SPEC.get(_type(ev.event_type))
    return (spec is not None and _group_ok(ident, spec) and _overlap(ident, ev)
            and abs(claim.event_date - ev.event_date) <= _window(claim))


def _missing_required(claim: Claim) -> bool:
    return not claim.actors or not has_required(claim)


def _prize_open(ev: EventRecord) -> bool:
    """A scoop-opened event no confirmed scoop or trusted claim has taken yet."""
    return (ev.first_claim_id is None and ev.confidence == "unverified"
            and ev.first_seen_ms is not None)


def _trusted(trust: Optional[float]) -> bool:
    return trust is not None and trust >= PRIZE_TRUST_MIN


def same_fact(a, f) -> bool:
    if isinstance(a.value, str) or isinstance(f.value, str):
        return str(a.value).strip().lower() == str(f.value).strip().lower()
    return claims_match(a, f)


def _type(value: str) -> Optional[EventType]:
    try:
        return EventType(value)
    except ValueError:
        return None


async def decide(store: Store, claim: Claim, entities: EntityIndex, places: PlaceIndex,
                 now_ms: int, trust: Optional[float] = None,
                 filer: Optional[str] = None) -> Novelty:
    ident = identity(claim, entities, places, filer)

    def known(ev: EventRecord, cands=(), prize: bool = True) -> Novelty:
        ids = [c.id for c in cands]
        if ev.confidence == "line":
            return Novelty(NEW, _new_route(trust), ev.id, line=True, candidates=ids)
        if prize and _prize_open(ev) and _trusted(trust):
            return Novelty(NEW, "event", ev.id, candidates=ids)
        return Novelty(KNOWN, None, ev.id, "known_event", candidates=ids)

    async def rival(ev: EventRecord, n: Novelty) -> Novelty:
        """A scoop naming another outcome than every scoop held on the event is held too."""
        if (n.route is None and not _trusted(trust) and _prize_open(ev)
                and await contests(store, claim, ev, entities)):
            return replace(n, route="scoop", reason=None)
        return n

    hit = await store.event_by_identity([ident.hard_key] if ident.hard_key else [],
                                        ident.fingerprint)
    if hit is not None:
        if not _attached(ident, claim, hit):
            return Novelty(KNOWN, None, None, "unsupported")
        return await rival(hit, known(hit, [hit]))

    found = await _candidates(store, claim, ident)
    cands = [c.id for c in found]

    # Identity first, then a re-worded copy by its figures; known events win over open lines.
    matched = ([c for c in found if _matches(ident, c)]
               or [c for c in found if _same_numbers(ident, claim, c)])
    if matched:
        ev = max(matched, key=lambda c: (c.confidence != "line", -c.id))
        return await rival(ev, known(ev, found))
    # An unverified CIK finds an event keyed by that CIK, but never takes its open prize.
    by_cik = [c for c in found if ident.claimed and _matches(ident, c, ident.claimed)]
    if by_cik:
        ev = max(by_cik, key=lambda c: (c.confidence != "line", -c.id))
        return known(ev, found, prize=False)

    if _missing_required(claim):
        return Novelty(KNOWN, None, None, "invalid_schema", candidates=cands)
    if not _trusted(trust) and any(entities.unclear(a) for a in claim.actors):
        return Novelty(KNOWN, None, None, "unclear_entity", candidates=cands)
    return Novelty(NEW, _new_route(trust), candidates=cands)


async def contests(store: Store, claim: Claim, ev: EventRecord, entities: EntityIndex) -> bool:
    """Scoops are held on the event, and the claim conflicts with each of them."""
    held = [c for c in await store.claims_for_event(ev.id, ("verified",)) if c.route == "scoop"]
    return bool(held) and all(conflicting(claim, Claim.model_validate(c.payload), entities)
                              for c in held)


async def _candidates(store: Store, claim: Claim, ident: Identity) -> List[EventRecord]:
    window = _window(claim)
    return await store.event_candidates(
        claim.event_date - window, claim.event_date + window,
        sorted(ident.actor_keys | ident.claimed),
        claim.event_type.value, claim.event_date)


async def near_events(store: Store, claim: Claim, entities: EntityIndex, places: PlaceIndex,
                      exclude: Iterable[int] = ()) -> List[EventRecord]:
    """Known events near a claim, for the post-fetch contrastive check."""
    skip = set(exclude)
    found = await _candidates(store, claim, identity(claim, entities, places))
    return [e for e in found if e.confidence != "line" and e.id not in skip]


async def find_known(store: Store, claim: Claim, entities: EntityIndex,
                     places: PlaceIndex) -> Optional[EventRecord]:
    """The known event a claim describes, by the same identity rules as `decide`."""
    ident = identity(claim, entities, places)
    hit = await store.event_by_identity([ident.hard_key] if ident.hard_key else [],
                                        ident.fingerprint)
    if hit is not None and hit.confidence != "line" and _attached(ident, claim, hit):
        return hit
    found = [c for c in await _candidates(store, claim, ident)
             if c.confidence != "line"
             and (_matches(ident, c) or _matches(ident, c, ident.claimed))]
    return min(found, key=lambda c: c.id) if found else None


def _new_route(trust: Optional[float]) -> str:
    return "event" if _trusted(trust) else "scoop"


async def open_line(store: Store, claim: Claim, entities: EntityIndex,
                    places: PlaceIndex, filer: Optional[str] = None) -> EventRecord:
    """Index a new claim as an open line, so rival claims for the same event queue behind it."""
    ident = identity(claim, entities, places, filer)
    rec = EventRecord(
        event_type=claim.event_type.value, category=ident.category, event_date=claim.event_date,
        fingerprint=ident.fingerprint, identity_key=ident.hard_key,
        headline=headline(claim.event_type, [a.name or a.id for a in claim.actors], claim.stage),
        actor_keys=tuple(sorted(ident.actor_keys)), names=ident.names,
        subject_keys=tuple(sorted(ident.subject)), numbers=numbers(claim),
        place_key=ident.place_key,
        stage=claim.stage.value if claim.stage else None, confidence="line")
    return await store.add_event(rec)


async def place(store: Store, claim: Claim, entities: EntityIndex, places: PlaceIndex,
                now_ms: int, trust: Optional[float] = None,
                filer: Optional[str] = None) -> Novelty:
    """The claim's novelty, opening a line when it is the first for a new event."""
    n = await decide(store, claim, entities, places, now_ms, trust, filer)
    if n.state == NEW and n.event_id is None:
        try:
            n.event_id = (await open_line(store, claim, entities, places, filer)).id
        except DuplicateEvent:
            # Another worker opened the same line first.
            n = await decide(store, claim, entities, places, now_ms, trust, filer)
    return n


async def apply(store: Store, row: ClaimRow, entities: EntityIndex, places: PlaceIndex,
                now_ms: int, trust: Optional[float] = None,
                filer: Optional[str] = None) -> ClaimRow:
    """queued -> novelty for one claim."""
    n = await place(store, Claim.model_validate(row.payload), entities, places, now_ms, trust,
                    filer)
    if n.state == KNOWN and n.route is None:
        out = await intake.transition(store, row, "rejected", reason=n.reason or "known_event",
                                      novelty=n.state, event_id=n.event_id)
        if out.reason == "known_event" and _trusted(trust) and n.event_id is not None:
            ev = await store.get_event(n.event_id)
            if (ev is not None and ev.first_claim_id not in (None, row.id)
                    and ev.confidence != "confirmed"):
                await store.add_job(intake.REPORT_JOB, row.id, now_ms)
        return out
    return await intake.transition(store, row, "novelty", novelty=n.state, route=n.route,
                                   event_id=n.event_id)


# Post-fetch contrastive check.

def contrastive(page_text: str, candidates: Iterable[EventRecord],
                own: Iterable[str] = ()) -> Optional[int]:
    """A candidate the page itself names (its subject or hard ID) along with its type.

    `own` is the claim's subject names: when the page names one of them and none overlaps the
    candidate's subject, the page is about a different event."""
    text = f" {normalize_name(page_text)} "
    raw = (page_text or "").lower()
    mine = [n for n in (normalize_name(o) for o in own if o) if _has(text, n)]
    for ev in candidates:
        spec = TYPE_SPEC.get(_type(ev.event_type))
        if spec is None:
            continue
        # A one-letter name (a ticker such as "A") is a word on any page.
        subject = [k.split(":", 1)[1] for k in ev.subject_keys
                   if k.startswith("name:") and len(k) > len("name:") + 1]
        named = any(_has(text, s) for s in subject)
        if named and mine and not any(_has(f" {m} ", s) or _has(f" {s} ", m)
                                      for m in mine for s in subject):
            named = False
        if ev.identity_key:
            named = named or ev.identity_key.split(":", 1)[-1].lower() in raw
        if named and any(_has(text, normalize_name(k)) for k in spec.keywords):
            return ev.id
    return None


def _has(text: str, phrase: str) -> bool:
    # The substring test is a fast path: the pattern can only match where the phrase occurs.
    return (bool(phrase) and phrase in text
            and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None)


# One event prize per source document.

_SHINGLE = re.compile(r"\w+")


_PRIME = (1 << 61) - 1
_PERMS = [tuple(int.from_bytes(hashlib.blake2b(f"{i}:{j}".encode(), digest_size=8).digest(),
                               "big") % _PRIME or 1 for j in (0, 1)) for i in range(256)]
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _grams(words: Sequence[str]) -> Set[int]:
    return {int.from_bytes(hashlib.blake2b(" ".join(words[i:i + 3]).encode(),
                                           digest_size=8).digest(), "big")
            for i in range(max(1, len(words) - 2))}


def minhash(text: str) -> List[int]:
    """MinHash of the body's 3-word shingles, with the head and tail trimmed."""
    words = _SHINGLE.findall((text or "").lower())
    cut = int(len(words) * DOC_TRIM)
    grams = _grams(words[cut:len(words) - cut] or words)
    k = min(DOC_MINHASH_K, len(_PERMS))
    return [min((a * g + b) % _PRIME for g in grams) & 0xFFFFFFFF for a, b in _PERMS[:k]]


def _lead(text: str) -> int:
    """Hash of the sentence after a wire credit, or 0 when the page carries none."""
    names = "|".join(re.escape(c) for c in WIRE_CREDITS)
    m = re.search(rf"\(\s*(?:{names})\s*\)\s*[-\u2013\u2014:]?", text or "", re.IGNORECASE)
    if m is None:
        return 0
    end = _SENTENCE_END.search(text, m.end())
    words = _SHINGLE.findall(text[m.end():end.start() if end else len(text)].lower())
    if len(words) < DOC_LEAD_MIN_WORDS:
        return 0
    return int.from_bytes(hashlib.blake2b(" ".join(words).encode(), digest_size=8).digest(),
                          "big") or 1


def doc_hash(canonical_url: str, text: str) -> str:
    url = hashlib.sha256(clean_url(canonical_url or "").encode()).hexdigest()[:16]
    return f"{url}{_lead(text):016x}" + "".join(f"{v:08x}" for v in minhash(text))


def same_doc(a: str, b: str) -> bool:
    if a[:16] == b[:16]:
        return True
    if len(a) != len(b) or len(a) <= 32:
        return False
    if a[16:32] == b[16:32] and int(a[16:32], 16):
        return True
    sa, sb = a[32:], b[32:]
    k = len(sa) // 8
    same = sum(sa[i:i + 8] == sb[i:i + 8] for i in range(0, len(sa), 8))
    return k > 0 and same / k >= DOC_SAME_JACCARD


def doc_probe(doc: str) -> Tuple[str, str, int, List[str]]:
    """What any document `same_doc` matches must share with this one: its URL hash, or at its
    length the wire lead or one of the first minhash slots any match agrees on."""
    sig = doc[32:]
    k = len(sig) // 8
    lead = doc[16:32] if len(doc) > 32 and int(doc[16:32] or "0", 16) else ""
    # A match agrees on at least `need` slots, so on one of any k - need + 1 of them.
    need = next((n for n in range(k + 1) if n / k >= DOC_SAME_JACCARD), k + 1) if k else 0
    slots = k - need + 1 if k else 0
    return doc[:16], lead, len(doc), [sig[i * 8:i * 8 + 8] for i in range(slots)]


async def doc_prize_open(store: Store, doc: str, now_ms: int, listing: bool = False) -> bool:
    """False once this document (or a syndicated copy) has used its event prizes."""
    since = now_ms - int(2 * MAX_AGE_D * 24 * HOUR_MS)
    used = sum(same_doc(doc, d) for d in await store.prize_docs(since, doc_probe(doc)))
    return used < (LISTING_PRIZES_MAX if listing else 1)

