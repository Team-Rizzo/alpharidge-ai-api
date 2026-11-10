"""Entity and place linking: actors to (scheme, id), places to a coarse key."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from alpharidge_ai.schema import TYPE_SPEC, Actor, EventType, IdScheme, Place
from alpharidge_ai.schema.types import TYPES_PATH
from alpharidge_ai.verify.floor import normalize

SEED_PATH = TYPES_PATH.with_name("entities.json")
OVERRIDES_PATH = Path(os.getenv("EL_ENTITY_OVERRIDES", "")
                      or Path(__file__).resolve().parent.parent / "data" / "entity_overrides.json")

LINKED, UNCLEAR = "linked", "unclear"

_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "plc", "ltd",
    "limited", "llc", "lp", "sa", "ag", "nv", "se", "group", "holdings", "holding",
}
# Public-role links in xrefs ("office:Q11696", "team:Q9617"); not identifiers.
ROLE_SCHEMES = ("office", "team")
_NOT_IDS = ("ticker", *ROLE_SCHEMES)
_ANCHORS = (IdScheme.CIK.value, IdScheme.LEI.value)
_DROP = re.compile(r"['.]")
_PUNCT = re.compile(r"[^\w\s]")


def normalize_name(name: Optional[str]) -> str:
    """Folded form used for alias matching: no case, accents, punctuation or legal suffix."""
    text = normalize(name).text.replace("&", " and ")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    words = _PUNCT.sub(" ", _DROP.sub("", text)).split()
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def canonical_id(scheme: str, external_id: str) -> str:
    external_id = external_id.strip()
    if scheme == IdScheme.CIK.value and external_id.isdigit():
        return external_id.zfill(10)
    if scheme in (IdScheme.WIKIDATA.value, IdScheme.LEI.value):
        return external_id.upper()
    return external_id


def id_key(scheme: str, external_id: str) -> str:
    return f"{scheme}:{canonical_id(scheme, external_id)}"


@dataclass(frozen=True)
class EntityRecord:
    scheme: str
    external_id: str
    name: str
    kind: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    xrefs: Tuple[str, ...] = ()
    website: Optional[str] = None
    id: Optional[int] = None
    status: str = LINKED
    origin: str = "seed"
    revision: Optional[str] = None
    checked_ms: Optional[int] = None

    @property
    def key(self) -> str:
        return id_key(self.scheme, self.external_id)

    @property
    def keys(self) -> Tuple[str, ...]:
        others = (id_key(*x.split(":", 1)) for x in self.xrefs
                  if ":" in x and x.split(":", 1)[0] not in _NOT_IDS)
        return (self.key, *others)

    @property
    def roles(self) -> Tuple[str, ...]:
        return tuple(x for x in self.xrefs if x.split(":", 1)[0] in ROLE_SCHEMES)

    @property
    def anchored(self) -> bool:
        return self.scheme in _ANCHORS or any(x.split(":", 1)[0] in _ANCHORS for x in self.xrefs)


def refreshes(old: EntityRecord, new: EntityRecord) -> bool:
    """A registry record replaces an earlier unclear answer for the same ID."""
    return (old.key == new.key and old.status == UNCLEAR
            and old.origin == new.origin == "registry")


def names_of(record: EntityRecord) -> Set[str]:
    """Folded name, aliases and tickers of a record."""
    tickers = (x.split(":", 1)[1] for x in record.xrefs if x.startswith("ticker:"))
    return {n for n in map(normalize_name, (record.name, *record.aliases, *tickers)) if n}


class SecNames:
    """The SEC's ticker list: each filer's registered name and tickers."""

    def __init__(self):
        self.names: Dict[str, str] = {}
        self.ciks: Dict[str, str] = {}
        self.tickers: Dict[str, List[str]] = {}

    def update(self, rows: Iterable[Tuple[str, str, str]]) -> None:
        """(CIK, ticker, registered name) rows; a ticker two filers share is dropped."""
        names, ciks, shared = {}, {}, set()
        for cik, ticker, name in rows:
            cik, ticker = str(cik).zfill(10), str(ticker).strip().upper()
            names.setdefault(cik, str(name))
            if ticker in ciks and ciks[ticker] != cik:
                shared.add(ticker)
            ciks.setdefault(ticker, cik)
        if names:
            self.names = names
            self.ciks = {t: c for t, c in ciks.items() if t not in shared}
            self.tickers = {}
            for t, c in self.ciks.items():
                self.tickers.setdefault(c, []).append(t)

    def name_of_ticker(self, ticker: Optional[str]) -> Optional[str]:
        """The filer's name for a ticker as a miner may write a company: upper case, no
        spaces."""
        t = (ticker or "").strip()
        if not (2 <= len(t) <= 6 and t.isupper() and t.replace(".", "").isalnum()):
            return None
        cik = self.ciks.get(t)
        return self.names.get(cik) if cik else None

    def tickers_of(self, cik: str) -> List[str]:
        return list(self.tickers.get(str(cik).zfill(10), ()))


SEC_NAMES = SecNames()


@dataclass(frozen=True)
class Link:
    entity: EntityRecord
    via: str  # "id" or "alias"


class EntityIndex:
    def __init__(self, records: Iterable[EntityRecord] = ()):
        self._by_key: Dict[str, EntityRecord] = {}
        self._by_alias: Dict[str, Set[str]] = {}
        self._by_ticker: Dict[str, Set[str]] = {}
        for r in records:
            self.add(r)

    def __len__(self) -> int:
        return len({r.key for r in self._by_key.values()})

    def add(self, record: EntityRecord) -> None:
        linked = record.status == LINKED
        for k in record.keys:
            if linked or k.startswith(f"{record.scheme}:"):
                old = self._by_key.get(k)
                if old is None or refreshes(old, record):
                    self._by_key[k] = record
        if not linked:
            return
        for alias in (record.name, *record.aliases):
            norm = normalize_name(alias)
            if norm:
                self._by_alias.setdefault(norm, set()).add(record.key)
        for x in record.xrefs:
            if x.startswith("ticker:"):
                self._by_ticker.setdefault(x[7:].strip().upper(), set()).add(record.key)

    def get(self, scheme: str, external_id: str) -> Optional[EntityRecord]:
        return self._by_key.get(id_key(scheme, external_id))

    def has(self, scheme: str, external_id: str) -> bool:
        return id_key(scheme, external_id) in self._by_key

    def by_alias(self, name: Optional[str]) -> Optional[EntityRecord]:
        """The single entity with this alias, or the single cross-referenced one; None when
        absent or ambiguous."""
        hits = [self._by_key[k] for k in sorted(self._by_alias.get(normalize_name(name), ()))]
        if len(hits) > 1:
            hits = [r for r in hits if r.anchored]
        return hits[0] if len(hits) == 1 else None

    def by_ticker(self, ticker: Optional[str]) -> Optional[EntityRecord]:
        hits = self._by_ticker.get((ticker or "").strip().upper(), set())
        return self._by_key[next(iter(hits))] if len(hits) == 1 else None

    def link(self, actor: Actor, event_type: Optional[EventType] = None) -> Optional[Link]:
        """An ID links only to a linked record of a kind the type allows that also carries
        the actor's name."""
        scheme = IdScheme(actor.id_scheme).value
        if scheme != IdScheme.OTHER.value:
            hit = self.get(scheme, actor.id)
            if (hit is not None and hit.status == LINKED and fits(hit, event_type)
                    and normalize_name(actor.name) in names_of(hit)):
                return Link(hit, "id")
        hit = self.by_alias(actor.name or (actor.id if scheme == IdScheme.OTHER.value else None))
        return Link(hit, "alias") if hit is not None else None

    def bound(self, actor: Actor, event_type: Optional[EventType] = None) -> bool:
        link = self.link(actor, event_type)
        return link is not None and link.via == "id"

    def unclear(self, actor: Actor) -> bool:
        """True when the actor's ID is a record that could not be established."""
        scheme = IdScheme(actor.id_scheme).value
        hit = self.get(scheme, actor.id) if scheme != IdScheme.OTHER.value else None
        return hit is not None and hit.status == UNCLEAR

    def actor_keys(self, actor: Actor, event_type: Optional[EventType] = None) -> Set[str]:
        """Identity keys for any-overlap matching: linked IDs and every folded name."""
        scheme = IdScheme(actor.id_scheme).value
        keys: Set[str] = set()
        link = self.link(actor, event_type)
        if link is not None:
            keys.update(link.entity.keys)
            keys.update(f"name:{n}" for n in names_of(link.entity))
        name = normalize_name(actor.name or (actor.id if scheme == IdScheme.OTHER.value else ""))
        if name:
            keys.add(f"name:{name}")
        filer = SEC_NAMES.name_of_ticker(actor.name) if link is None else None
        if filer and normalize_name(filer):
            keys.add(f"name:{normalize_name(filer)}")
        return keys

    @classmethod
    def from_seed(cls, path: Path = SEED_PATH,
                  overrides: Optional[Path] = OVERRIDES_PATH) -> "EntityIndex":
        first = read_seed(overrides, "override") if overrides and overrides.exists() else []
        return cls([*first, *read_seed(path)])


_shared: Optional[EntityIndex] = None


def shared_index() -> EntityIndex:
    """The process-wide index: overrides, then the seed, then cached registry records."""
    global _shared
    if _shared is None:
        _shared = EntityIndex.from_seed()
    return _shared


def fits(record: EntityRecord, event_type: Optional[EventType]) -> bool:
    """The record's kind is one the type takes as an actor (any kind when it lists none)."""
    if event_type is None:
        return True
    kinds = TYPE_SPEC[event_type].actor_roles
    return not kinds or record.kind in kinds


def read_seed(path: Path = SEED_PATH, origin: str = "seed") -> List[EntityRecord]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [EntityRecord(scheme=e["scheme"], external_id=e["id"], name=e["name"],
                         kind=e.get("kind"), aliases=tuple(e.get("aliases", ())),
                         xrefs=tuple(e.get("xrefs", ())), website=e.get("website"),
                         origin=origin)
            for e in data["entities"]]


class PlaceIndex:
    """Place keys at a type's level. Regions are not resolved, so a region level keys by
    country."""

    def place_key(self, place: Optional[Place], level: str) -> Optional[str]:
        """'' when the type ignores place, the country code, or None when unknown."""
        if level == "none":
            return ""
        return None if place is None else place.iso2


def places_match(a: Optional[str], b: Optional[str]) -> bool:
    """Coarse match; an unknown or country-only key matches anything inside it."""
    if a is None or b is None or a == "" or b == "":
        return True
    return a == b or a == b.split("-", 1)[0] or b == a.split("-", 1)[0]


def record_from(m) -> EntityRecord:
    return EntityRecord(scheme=m.scheme, external_id=m.externalId, name=m.name, kind=m.kind,
                        aliases=tuple(m.aliases or ()), xrefs=tuple(m.xrefs or ()),
                        website=m.website, id=m.id, status=m.status, origin=m.origin,
                        revision=m.revision,
                        checked_ms=round(m.checkedAt.timestamp() * 1000) if m.checkedAt else None)


async def seed_entities(db, records: Iterable[EntityRecord]) -> int:
    n = 0
    for r in records:
        ext = canonical_id(r.scheme, r.external_id)
        data = {"name": r.name, "kind": r.kind, "aliases": list(r.aliases), "xrefs": list(r.xrefs),
                "website": r.website, "status": r.status, "origin": r.origin,
                "revision": r.revision}
        await db.entity.upsert(
            where={"uq_entity_scheme_external": {"scheme": r.scheme, "externalId": ext}},
            data={"create": {"scheme": r.scheme, "externalId": ext, **data}, "update": data})
        n += 1
    return n

