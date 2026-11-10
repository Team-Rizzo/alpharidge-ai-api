"""Source trust registry: source keys, page rules, owner clusters and per-category trust.

A source's trust is the base set by the seed, the catalogue or an operator, less its
penalties. `nightly` recomputes the penalties from SourceEvent rows; operator changes and
bans apply at once and are written to the trust log.
"""

from __future__ import annotations

import asyncio
import csv
import fnmatch
import html
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from tld import get_fld

from alpharidge_ai.schema import TYPE_SPEC, Claim, EventType, IdScheme
from alpharidge_ai.verify.fetch import clean_url
from alpharidge_ai.verify.filing import filing_row, submissions_url
from alpharidge_ai.verify.names import abbreviates
from app import ROOT
from app.config import CATEGORIES, EVENT_LANE
from app.services.entities import (
    SEC_NAMES, EntityIndex, EntityRecord, canonical_id, id_key, normalize_name,
)
from app.services.ledger import clawback, window_ms
from app.services.store import (
    ClaimRow, OwnerRow, PageRuleRow, SourceEventRow, SourceRow, SourceTrustRow, Store,
    TrustLogRow,
)
from app.utils.clock import DAY_MS, now_ms as _now_ms

PRIZE_TRUST_MIN = 0.5
TRUST_BASE_OFFICIAL = 1.0
TRUST_BASE_CATALOGUE = 0.7
TRUST_BASE_OTHER = 0.1
TRUST_HALF_LIFE_D = 90
TRUST_PEN_ANSWER_KEY = 0.10
TRUST_PEN_CONTRADICT = 0.05
TRUST_PEN_RETRACTION = 0.10
TRUST_PEN_CHANGED = 0.20
TRUST_CONTRADICT_MIN_OWNERS = 2
TRUST_REPORTED_MIN = 0.3
# Hosting platforms whose subdomains belong to different publishers.
TRUST_SHARED_DOMAINS = (
    "wordpress.com", "tumblr.com", "ghost.io", "beehiiv.com", "weebly.com", "squarespace.com",
    "wixsite.com", "substack.com", "medium.com", "blogspot.com", "github.io", "github.com",
    "gitlab.io", "netlify.app", "vercel.app", "pages.dev", "herokuapp.com", "sites.google.com",
    "docs.google.com", "blogger.com", "typepad.com", "livejournal.com", "linkedin.com",
    "facebook.com", "x.com", "twitter.com", "instagram.com", "youtube.com", "reddit.com",
    "notion.site")
# Highest trust for pages under "wire" page rules, unless primary or officially seeded.
TRUST_WIRE_MAX = 0.45
# Filing archive whose per-filer paths count as that filer's own site.
TRUST_FILING_HOST = "sec.gov"
# Roles on a filing's index page that make a company its filer; "Subject" is not one.
TRUST_FILER_ROLES = ("filer", "filed by")
TRUST_FILING_CACHE_MAX = 5000
# The SEC's CIK and ticker list, read once a day, for an actor named by the filer's ticker.
TRUST_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TRUST_TICKERS_TTL_S = 86400
TRUST_TICKERS_RETRY_S = 600
# A filer's submissions list, kept this long.
TRUST_SUBMISSIONS_TTL_S = 600
TRUST_RULES_CACHE_S = 60

SEED_PATH = ROOT / "app" / "data" / "trust_seed.json"
CATALOGUE = ROOT / "app" / "data" / "catalogue.csv"

ACTIVE, PENDING, SUSPENDED, BANNED = "active", "pending", "suspended", "banned"
BLOCKED = (SUSPENDED, BANNED)

FIRST, CONFIRM, CONTRADICT = "first", "confirm", "contradict"
WIRE = "wire"
ANSWER_KEY, RETRACTION, CHANGED = "answer_key", "retraction", "changed"
PENALTIES = {ANSWER_KEY: TRUST_PEN_ANSWER_KEY, CONTRADICT: TRUST_PEN_CONTRADICT,
             RETRACTION: TRUST_PEN_RETRACTION, CHANGED: TRUST_PEN_CHANGED}
CLAWED = {RETRACTION: "clawed:retraction", CHANGED: "clawed:changed", BANNED: "clawed:ban",
          ANSWER_KEY: "clawed:answer_key", CONTRADICT: "clawed:contradicted"}
# Claims whose prize is still in the open window.
OPEN_STATUSES = ("verified", "provisional")
TRUSTED_BANDS = ("primary", "prize-eligible")
CONTRADICTED, RETRACTED, CORRECTED = "contradicted", "retracted", "corrected"
# Trust-log flag for a trusted page that disagrees with a claim, for the operator.
FLAG_CONTRADICTED = "flag:contradicted"
MARKS = {ANSWER_KEY: CONTRADICTED, CONTRADICT: CONTRADICTED, RETRACTION: RETRACTED}


# Source keys

def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def domain_of(url: str) -> str:
    """Registrable domain; the bare host when it has none (an IP or localhost)."""
    host = host_of(url)
    return (get_fld(f"http://{host}", fail_silently=True) or host) if host else ""


def path_of(url: str) -> str:
    return urlsplit(clean_url(url)).path or "/"


def is_shared(host: str) -> bool:
    """A hosting platform whose subdomains or paths belong to different publishers."""
    host = host.lower().lstrip("*.")
    return any(host == d or host.endswith("." + d) for d in TRUST_SHARED_DOMAINS)


def parse_source(text: str) -> Tuple[str, str, str]:
    """"forbes.com/sites/", "*.example.com" or a URL as (domain, host, path rule)."""
    wild = text.startswith("*.")
    text = text[2:] if wild else text
    url = text if "://" in text else f"http://{text}"
    path = urlsplit(url).path
    host = host_of(url)
    return domain_of(url), ("*." + host if wild else host), "" if path in ("", "/") else path


def label(source: SourceRow) -> str:
    return source.host + source.path_rule


def covers_path(rule: str, path: str) -> bool:
    if not rule:
        return True
    base = rule.rstrip("/")
    return path == base or path.startswith(base + "/")


def covers_host(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern


def match(rows: Iterable[SourceRow], url: str) -> Optional[SourceRow]:
    """The source for the URL: its exact host before any "*." row, then the longest path."""
    host, path = host_of(url), path_of(url)
    hits = [r for r in rows if covers_host(r.host, host) and covers_path(r.path_rule, path)]
    return max(hits, key=lambda r: (not r.host.startswith("*."), len(r.host),
                                    len(r.path_rule))) if hits else None


# Page rules

def rule_matches(pattern: str, url: str) -> bool:
    """"*/tag/*" matches the path anywhere on any host; "forbes.com/sites/*" is anchored."""
    host, path = host_of(url), path_of(url).lower()
    dom, _, glob = pattern.lower().partition("/")
    glob = "/" + glob
    if dom == "*":
        return fnmatch.fnmatchcase(path, "*" + glob)
    return (host == dom or host.endswith("." + dom)) and fnmatch.fnmatchcase(path, glob)


def _specificity(rule: PageRuleRow) -> Tuple[bool, int]:
    return not rule.pattern.startswith("*"), len(rule.pattern)


@dataclass(frozen=True)
class Subject:
    """Whom an official row speaks for: entity IDs ("wikidata:Q312") and names."""
    ids: Tuple[str, ...] = ()
    names: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OfficialSeed:
    host: str
    path: str
    kind: str
    categories: Tuple[str, ...]
    # None: official for any claim on the row (a government, a league).
    subject: Optional[Subject] = None


@dataclass(frozen=True)
class Seed:
    official: Tuple[OfficialSeed, ...] = ()
    rules: Tuple[PageRuleRow, ...] = ()


def _subject(raw: Optional[dict]) -> Optional[Subject]:
    if not raw:
        return None
    ids = tuple(id_key(*i.split(":", 1)) for i in raw.get("ids", ()))
    names = tuple(raw.get("names", ()))
    if not ids and not names or not all(n.strip() for n in names):
        raise ValueError(f"empty subject: {raw}")
    return Subject(ids, names)


def read_seed(path: Path = SEED_PATH) -> Seed:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return Seed(
        tuple(OfficialSeed(o["host"], p, o.get("kind", ""), tuple(o["categories"]),
                           _subject(o.get("subject")))
              for o in data.get("official", ()) for p in o.get("paths") or ("",)),
        tuple(PageRuleRow(r["pattern"], r["kind"], r["reason"]) for r in data.get("page_rules", ())))


def read_catalogue(path: Path = CATALOGUE) -> List[Tuple[str, str, Tuple[str, ...]]]:
    """(domain, path, categories) rows from a news catalogue export (CSV)."""
    with open(path, encoding="utf-8", newline="") as f:
        return [(r["domain"].strip().lower(), (r.get("path") or "").strip(),
                 tuple(c for c in (r.get("categories") or "").split(";") if c))
                for r in csv.DictReader(f) if r.get("domain")]


_FILING_PATH = re.compile(r"/Archives/edgar/data/(\d+)/(\d{18}|\d{10}-\d{2}-\d{6})")
_INDEX_COMPANY = re.compile(
    r'class="companyName">([^<]*)\(([^()<]+)\)\s*<acronym[^>]*>CIK</acronym>:\s*<a[^>]*>\s*(\d+)')


def filing_of(url: str) -> Optional[Tuple[str, str]]:
    """(CIK folder, accession) of a page on the filing archive."""
    if host_of(url) != TRUST_FILING_HOST:
        return None
    m = _FILING_PATH.match(path_of(url))
    return (m.group(1).zfill(10), m.group(2).replace("-", "")) if m else None


def filers(text: str) -> Dict[str, str]:
    """CIK -> registered name of each company a filing index page lists as filer."""
    return {m.group(3).zfill(10): html.unescape(m.group(1)).strip()
            for m in _INDEX_COMPANY.finditer(text)
            if m.group(2).strip().lower() in TRUST_FILER_ROLES}


def _bare(word: str) -> str:
    """A word without a possessive s: "carters" for CARTERS INC or Carter's is "carter"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def names_filer(name: Optional[str], registered: str) -> bool:
    """The name is the filer's registered name, its first word alone, its initials ("UPS"
    for United Parcel Service) or another shorter form ("FHLBank Chicago" for Federal Home
    Loan Bank of Chicago, `names.abbreviates`), with or without a possessive ("Carter" for
    CARTERS INC)."""
    full = normalize_name(registered)
    words = [w for w in full.split() if w not in ("of", "and")]
    mine = normalize_name(name)
    initials = "".join(w[0] for w in words) if len(words) > 1 else None
    joined = full.replace(" ", "")
    return bool(mine) and (mine == initials or mine.replace(" ", "") == joined
                           or _bare(mine) in (_bare(words[0]) if words else None, _bare(joined))
                           or abbreviates(name or "", registered))


def filer_roles(event_type) -> set:
    """Roles of the type's subject (for a deal the acquirer or the target): a filing counts as
    the claim's own only when one of them filed it."""
    spec = TYPE_SPEC[EventType(event_type)]
    return {*spec.subject_roles, *((spec.subject_slot,) if spec.subject_slot else ())}


class Filings:
    """Whether a filing was made by a CIK, read from its index page and cached per
    accession. Off without the SEC user agent; an unreadable index page is a no. The index
    page comes from the page fetcher, which reads it for the filing's publish time; the
    ticker and submissions lists are read with the SEC user agent."""

    def __init__(self, fetcher=None, sec=None):
        self._fetcher = fetcher
        self._sec = sec
        self._filers: Dict[str, Dict[str, str]] = {}
        self._tickers: Dict[str, set] = {}
        self._tickers_due = 0.0
        self._subs: Dict[str, Tuple[float, Optional[dict]]] = {}
        self._lock = asyncio.Lock()

    @property
    def sec(self):
        if self._sec is None:
            from app.services.answer_key.base import FeedClient, feed_config
            self._sec = FeedClient(feed_config(EVENT_LANE["AK_SEC_USER_AGENT"]))
        return self._sec

    @property
    def fetcher(self):
        return self._fetcher or self.sec

    async def tickers(self) -> Dict[str, set]:
        """CIK -> the filer's tickers; empty while the list can't be read."""
        if time.monotonic() >= self._tickers_due:
            try:
                rows = json.loads(await self.sec.get_text(TRUST_TICKERS_URL) or "{}")
                got: Dict[str, set] = {}
                for r in rows.values():
                    got.setdefault(str(r["cik_str"]).zfill(10), set()).add(str(r["ticker"]).upper())
                SEC_NAMES.update((r["cik_str"], r["ticker"], r.get("title") or "")
                                 for r in rows.values())
            except (ValueError, KeyError, TypeError, AttributeError):
                got = {}
            self._tickers = got or self._tickers
            self._tickers_due = time.monotonic() + (TRUST_TICKERS_TTL_S if got
                                                    else TRUST_TICKERS_RETRY_S)
        return self._tickers

    async def warm(self) -> None:
        """Reads the ticker list when it is due, with the SEC user agent set."""
        if EVENT_LANE["AK_SEC_USER_AGENT"]:
            async with self._lock:
                await self.tickers()

    async def filer(self, actors, url: str, event_type) -> Optional[str]:
        """The filer's CIK, when an actor in a subject role is a filer of the filing: by the CIK
        it carries, or without one by a name for the filer (`names_filer`) or its ticker. The
        CIK folder alone does not decide: a filing is also kept under its subject company's
        folder."""
        hit = filing_of(url)
        if not EVENT_LANE["AK_SEC_USER_AGENT"] or hit is None:
            return None
        mine = [a for a in actors if a.role in filer_roles(event_type)]
        if not mine:
            return None
        acc = hit[1]
        async with self._lock:
            if acc not in self._filers:
                raw = await self.fetcher.filing_index(url)
                if raw is None:
                    return None
                while len(self._filers) >= TRUST_FILING_CACHE_MAX:
                    del self._filers[next(iter(self._filers))]
                self._filers[acc] = filers(raw)
        found = self._filers[acc]
        for a in mine:
            if getattr(a, "id_scheme", None) == IdScheme.CIK:
                cik = canonical_id(IdScheme.CIK.value, a.id)
                if cik in found:
                    return cik
            else:
                cik = next((c for c, n in found.items() if names_filer(a.name, n)), None)
                if cik is None and a.name:
                    async with self._lock:
                        tickers = await self.tickers()
                    cik = next((c for c in found
                                if a.name.strip().upper() in tickers.get(c, ())), None)
                if cik:
                    return cik
        return None

    async def report(self, actors, url: str, event_type) -> Optional[dict]:
        """The filing's form, items and dates from its filer's submissions list, when an actor
        in a subject role filed it (`filer`)."""
        hit = filing_of(url)
        cik = await self.filer(actors, url, event_type) if hit else None
        if cik is None:
            return None
        async with self._lock:
            due, subs = self._subs.get(cik, (0.0, None))
            if time.monotonic() >= due:
                try:
                    subs = json.loads(await self.sec.get_text(submissions_url(cik)) or "null")
                except ValueError:
                    subs = None
                while len(self._subs) >= TRUST_FILING_CACHE_MAX:
                    del self._subs[next(iter(self._subs))]
                self._subs[cik] = (time.monotonic() + TRUST_SUBMISSIONS_TTL_S, subs)
        return filing_row(subs, cik, hit[1]) if isinstance(subs, dict) else None


class TrustRegistry:
    """Page rules from the seed file plus operator rules, cached for a short window."""

    def __init__(self, seed: Optional[Seed] = None, filings: Optional[Filings] = None):
        self.seed = seed if seed is not None else read_seed()
        self.rules: List[PageRuleRow] = list(self.seed.rules)
        self.subjects = {parse_source(o.host + o.path)[1:]: o.subject
                         for o in self.seed.official if o.subject is not None}
        # Self-serve release wires speak for whichever issuer posts on them.
        self.wires = {parse_source(o.host + o.path)[1:] for o in self.seed.official
                      if o.kind == WIRE}
        self._loaded: Optional[float] = None
        self.filings = filings or Filings()

    async def refresh(self, store: Store, force: bool = False) -> None:
        if (not force and self._loaded is not None
                and time.monotonic() - self._loaded < TRUST_RULES_CACHE_S):
            return
        merged = {r.pattern: r for r in self.seed.rules}
        merged.update({r.pattern: r for r in await store.page_rules()})
        self.rules = list(merged.values())
        self._loaded = time.monotonic()

    def rule_for(self, url: str, wire: bool = False) -> Optional[PageRuleRow]:
        """The most specific rule for the URL; "wire" rules are looked up on their own."""
        hits = [r for r in self.rules
                if (r.kind == WIRE) == wire and rule_matches(r.pattern, url)]
        return max(hits, key=_specificity) if hits else None

    def page_excluded(self, url: str, page=None) -> Optional[str]:
        urls = (url, getattr(page, "canonical_url", "") if page is not None else "")
        for u in dict.fromkeys(u for u in urls if u):
            rule = self.rule_for(u)
            if rule is not None and rule.kind == "exclude":
                return rule.reason
        return None

    def is_listing(self, url: str) -> bool:
        rule = self.rule_for(url)
        return rule is not None and rule.kind == "listing"

    def is_wire(self, url: str) -> bool:
        """Paid or self-serve releases republished on another site."""
        return self.rule_for(url, wire=True) is not None

    def speaks_for(self, source: SourceRow, claim: Claim, entities: EntityIndex) -> bool:
        """Whether an official row covers the claim: always, unless the row names a subject
        (a company newsroom, a club site); then one of the claim's parties must be it."""
        subject = self.subjects.get((source.host, source.path_rule))
        return subject is None or names_subject(claim, subject, entities)


def claim_names(claim: Claim, entities: EntityIndex) -> Tuple[set, List[str]]:
    """Identity keys and names of every party a claim names: its actors in any role (the
    acquirer, the clubs, the player's team) and its name slots (the target, the clubs a player
    moves between, the counterparties)."""
    spec = TYPE_SPEC[EventType(claim.event_type)]
    keys: set = set()
    names: List[str] = []
    for a in claim.actors:
        keys |= entities.actor_keys(a, claim.event_type)
        link = entities.link(a, claim.event_type)
        names += [a.name or a.id, *((link.entity.name, *link.entity.aliases) if link else ())]
    for f in claim.facts:
        if (isinstance(f.value, str)
                and spec.fields.get(f.field, {}).get("kind") not in ("qualifier", "date")):
            hit = entities.by_alias(f.value)
            keys |= set(hit.keys) if hit is not None else set()
            keys.add(f"name:{normalize_name(f.value)}")
            names.append(f.value)
    return keys, [n for n in names if n]


def names_subject(claim: Claim, subject: Subject, entities: EntityIndex) -> bool:
    """The claim names the subject: by an entity ID, or by a name form (`names_filer`)."""
    keys, names = claim_names(claim, entities)
    if keys & {*subject.ids, *(f"name:{normalize_name(n)}" for n in subject.names)}:
        return True
    return any(names_filer(n, s) for n in names for s in subject.names)


# Lookups on the claim path

def trust(source: SourceRow, category: str) -> float:
    if source.status in BLOCKED:
        return 0.0
    t = source.category_trust.get(category)
    return float(t) if t is not None else source.base_trust


def is_primary(source: SourceRow, entity: Optional[EntityRecord],
               url: Optional[str] = None) -> bool:
    """True when the page is on the entity's own official site, as the seed or an operator
    lists it."""
    if entity is None or source.status in BLOCKED or entity.origin == "registry":
        return False
    host = host_of(url) if url else source.host
    path = path_of(url) if url else (source.path_rule or "/")
    site = entity.website
    if not site:
        return False
    site = site if "://" in site else f"http://{site}"
    if host_of(site) != host or is_shared(host):
        return False
    return covers_path(path_of(site).rstrip("/"), path)


async def base_of(store: Store, source: SourceRow, category: str) -> float:
    """The starting trust set by the seed, the catalogue or an operator."""
    row = next((r for r in await store.source_trust(source.id) if r.category == category), None)
    return row.base if row is not None else source.base_trust


async def counts_as_owner(store: Store, source: SourceRow, category: str) -> bool:
    """Whether the source counts as a separate owner: a seeded or operator-set source."""
    return await base_of(store, source, category) >= PRIZE_TRUST_MIN


def band(t: float, primary: bool = False) -> str:
    if primary and t > 0:
        return "primary"
    if t >= PRIZE_TRUST_MIN:
        return "prize-eligible"
    return "reported" if t >= TRUST_REPORTED_MIN else "low"


@dataclass(frozen=True)
class ClaimTrust:
    t: float
    band: str
    primary: bool
    # The CIK the filer check verified, if any.
    filer: Optional[str] = None
    # Primary, or an official source that speaks for the claim: its page can settle it.
    official: bool = False


async def of_claim(store: Store, registry: TrustRegistry, entities: EntityIndex, claim: Claim,
                   source: SourceRow, category: str, url: str) -> ClaimTrust:
    """Trust for a claim's page: the source's trust, official on the primary actor's own site
    or in a filing an actor made under the CIK it carries, and capped on wire pages unless the
    source is officially seeded. An official row with a subject counts as an outlet for claims
    that don't name it."""
    link = entities.link(claim.actors[0], claim.event_type) if claim.actors else None
    filer = await registry.filings.filer(claim.actors, url, claim.event_type)
    filer = filer if filer and source is not None and source.status not in BLOCKED else None
    primary = is_primary(source, link.entity if link is not None else None, url) or bool(filer)
    t = trust(source, category)
    seeded = (source is not None
              and await base_of(store, source, category) >= TRUST_BASE_OFFICIAL)
    official = primary or (seeded and source.status not in BLOCKED
                           and (source.host, source.path_rule) not in registry.wires)
    if primary:
        t = max(t, TRUST_BASE_OFFICIAL)
    elif seeded and not registry.speaks_for(source, claim, entities):
        t, official = min(t, TRUST_BASE_CATALOGUE), False
    elif registry.is_wire(url) and not seeded:
        t = min(t, TRUST_WIRE_MAX)
    return ClaimTrust(round(t, 6), band(t, primary), primary, filer, official)


async def trusted_list(store: Store, registry: TrustRegistry,
                       categories: Sequence[str]) -> dict:
    """The sources whose pages can win an event, per category, and the page rules that apply
    on any source."""
    out = []
    for s in await store.sources():
        if s.status in BLOCKED:
            continue
        cats = {c: round(trust(s, c), 3) for c in categories if trust(s, c) >= PRIZE_TRUST_MIN}
        if not cats:
            continue
        key = (s.host, s.path_rule)
        official = any([await base_of(store, s, c) >= TRUST_BASE_OFFICIAL for c in cats])
        row = {"source": label(s), "trust": cats,
               "kind": WIRE if key in registry.wires else "official" if official else "outlet"}
        subject = registry.subjects.get(key)
        if official and subject is not None:
            row["official_for"] = list(subject.names)
        out.append(row)
    await registry.refresh(store)
    rules = [{"pattern": r.pattern, "kind": r.kind, "reason": r.reason}
             for r in sorted(registry.rules, key=lambda r: r.pattern) if r.kind != WIRE]
    return {"sources": sorted(out, key=lambda r: r["source"]), "page_rules": rules}


async def resolve(store: Store, url: str, now_ms: Optional[int] = None) -> SourceRow:
    """The registered source for a URL, or a new pending one for its host."""
    domain = domain_of(url)
    rows = await store.sources(domain)
    hit = match(rows, url)
    if hit is not None:
        return hit
    return await store.add_source(SourceRow(
        domain=domain, host=host_of(url), owner_id=await _owner_for(store, domain, rows, now_ms),
        base_trust=TRUST_BASE_OTHER))


async def _owner_for(store: Store, domain: str, rows: Sequence[SourceRow],
                     now_ms: Optional[int]) -> str:
    """Sources on one registrable domain share an owner."""
    owner_id = next((r.owner_id for r in rows if r.owner_id), None)
    if owner_id is None:
        owner_id = f"domain:{domain}"
        if await store.get_owner(owner_id) is None:
            await store.put_owner(OwnerRow(owner_id, {}, now_ms or _now_ms()))
    return owner_id


async def record(store: Store, role: str, source: SourceRow, category: str, at_ms: int,
                 event_id: Optional[int] = None, claim_id: Optional[int] = None,
                 published_ms: Optional[int] = None,
                 by: Optional[SourceRow] = None) -> SourceEventRow:
    return await store.add_source_event(SourceEventRow(
        source_id=source.id, role=role, category=category, at_ms=at_ms, event_id=event_id,
        claim_id=claim_id, owner_id=source.owner_id, published_ms=published_ms,
        by_source_id=by.id if by is not None else None))


async def confirm(store: Store, event_id: int, source: SourceRow, claim: ClaimRow,
                  published_ms: Optional[int] = None) -> SourceEventRow:
    return await record(store, CONFIRM, source, claim.category, claim.received_ms, event_id,
                        claim.id, published_ms)


async def contradict(store: Store, event_id: int, source: SourceRow, claim: ClaimRow,
                     by: SourceRow, at_ms: Optional[int] = None) -> SourceEventRow:
    """`by` reports something that contradicts `claim`, taken from `source`."""
    return await record(store, CONTRADICT, source, claim.category, at_ms or _now_ms(), event_id,
                        claim.id, by=by)


async def penalize(store: Store, kind: str, source: SourceRow, claim: ClaimRow,
                   now_ms: Optional[int] = None, head: Optional[int] = None) -> SourceEventRow:
    """A changed page against a used claim."""
    now_ms = now_ms or _now_ms()
    row = await record(store, kind, source, claim.category, now_ms, claim.event_id, claim.id)
    if kind == CHANGED:
        await log(store, "flag:changed", None, str(claim.id), "page changed after fetch",
                  "detector", source.id, now_ms=now_ms)
    if kind in CLAWED:
        fresh = await store.get_claim(claim.id)
        if fresh is not None:
            await clawback(store, [fresh], CLAWED[kind], head)
    return row


def clawable(row: ClaimRow, now_ms: int) -> bool:
    """Open, or settled with rows still in the ledger window."""
    if row.status in OPEN_STATUSES:
        return True
    return (row.status == "settled" and row.settle_ms is not None
            and row.settle_ms + window_ms() > now_ms)


async def proved_wrong(store: Store, kind: str, claim: ClaimRow, reason: str, actor: str,
                       now_ms: Optional[int] = None, head: Optional[int] = None,
                       mark: Optional[str] = None, mark_event: bool = True) -> bool:
    """A used claim found wrong by the answer key, an official page or a retraction: the
    source's penalty and the claw. The event is marked unless `mark_event` is off. False when
    the claim was already handled."""
    now_ms = now_ms or _now_ms()
    claim = await store.get_claim(claim.id)
    if claim is None or claim.wrong is not None:
        return False
    source = await store.get_source(claim.source_id) if claim.source_id is not None else None
    if source is not None and kind != CONTRADICT:
        await record(store, kind, source, claim.category, now_ms, claim.event_id, claim.id)
    clawed = clawable(claim, now_ms)
    if clawed:
        await clawback(store, [claim], CLAWED[kind], head)
        claim = await store.get_claim(claim.id)
    await store.update_claim(claim.id, claim.status, wrong=kind)
    await log(store, f"wrong:{kind}", claim.trust_band, f"clawed={clawed}",
              reason, actor, claim.source_id, now_ms=now_ms)
    ev = await store.get_event(claim.event_id) if claim.event_id is not None else None
    if mark_event and ev is not None and ev.marked is None:
        await store.update_event(ev.id, marked=mark or MARKS[kind])
    return True


# Trust log and operator changes

async def log(store: Store, field: str, old, new, reason: str, actor: str,
              source_id: Optional[int] = None, rule_id: Optional[int] = None,
              now_ms: Optional[int] = None) -> None:
    await store.add_trust_log(TrustLogRow(
        field=field, old=None if old is None else str(old), new=None if new is None else str(new),
        reason=reason, actor=actor, source_id=source_id, rule_id=rule_id,
        at_ms=now_ms or _now_ms()))


def _score(row: SourceTrustRow, status: str) -> float:
    if status in BLOCKED:
        return 0.0
    return round(min(1.0, max(0.0, row.base - row.penalties)), 6)


async def recache(store: Store, source: SourceRow) -> SourceRow:
    """Rewrite the per-category read cache from the stored SourceTrust rows."""
    cache = {r.category: _score(r, source.status) for r in await store.source_trust(source.id)}
    await store.update_source(source.id, category_trust=cache)
    return replace(source, category_trust=cache)


async def find(store: Store, text: str) -> Optional[SourceRow]:
    domain, host, path = parse_source(text)
    return next((r for r in await store.sources(domain)
                 if (r.host, r.path_rule) == (host, path)), None)


async def add(store: Store, text: str, base: float, reason: str, actor: str,
              categories: Sequence[str] = (), status: str = ACTIVE,
              now_ms: Optional[int] = None) -> SourceRow:
    """Register a source (or update an existing one) with a starting trust."""
    domain, host, path = parse_source(text)
    if host.startswith("*.") and is_shared(host):
        raise ValueError(f"shared hosting platform: {host}")
    source = await find(store, text)
    if source is None:
        rows = await store.sources(domain)
        source = await store.add_source(SourceRow(
            domain=domain, host=host, path_rule=path,
            owner_id=await _owner_for(store, domain, rows, now_ms),
            status=status, base_trust=TRUST_BASE_OTHER))
        await log(store, "source", None, label(source), reason, actor, source.id, now_ms=now_ms)
    if source.status != status and source.status not in BLOCKED:
        await store.update_source(source.id, status=status)
        await log(store, "status", source.status, status, reason, actor, source.id, now_ms=now_ms)
        source = replace(source, status=status)
    return await set_trust(store, source, base, reason, actor, categories, now_ms)


async def set_trust(store: Store, source: SourceRow, base: float, reason: str, actor: str,
                    categories: Sequence[str] = (), now_ms: Optional[int] = None) -> SourceRow:
    """Set the base trust in some categories, or in all of them."""
    rows = {r.category: r for r in await store.source_trust(source.id)}
    if not categories:
        await store.update_source(source.id, base_trust=base)
        await log(store, "base", source.base_trust, base, reason, actor, source.id, now_ms=now_ms)
        source = replace(source, base_trust=base)
    out = []
    for c in categories or sorted(set(CATEGORIES) | set(rows)):
        cur = rows.get(c) or SourceTrustRow(source.id, c, source.base_trust)
        if categories and cur.base != base:
            await log(store, f"base:{c}", cur.base, base, reason, actor, source.id, now_ms=now_ms)
        out.append(replace(cur, base=base, trust=_score(replace(cur, base=base), source.status)))
    await store.put_source_trust(out)
    return await recache(store, source)


async def set_status(store: Store, source: SourceRow, status: str, reason: str, actor: str,
                     now_ms: Optional[int] = None, head: Optional[int] = None) -> SourceRow:
    """Bans and suspensions apply at once; a ban also claws back open prizes."""
    if source.status != status:
        await store.update_source(source.id, status=status)
        await log(store, "status", source.status, status, reason, actor, source.id, now_ms=now_ms)
    source = await recache(store, replace(source, status=status))
    if status == BANNED:
        await clawback(store, await store.claims_by_source(source.id, OPEN_STATUSES),
                       CLAWED[BANNED], head)
    return source


async def add_rule(store: Store, pattern: str, kind: str, reason: str, actor: str,
                   now_ms: Optional[int] = None) -> PageRuleRow:
    rule = await store.add_page_rule(PageRuleRow(pattern, kind, reason, actor))
    await log(store, "rule", None, f"{kind} {pattern}", reason, actor, rule_id=rule.id,
              now_ms=now_ms)
    return rule


async def seed(store: Store, data: Optional[Seed] = None,
               catalogue: Optional[Path] = None, now_ms: Optional[int] = None) -> int:
    """Register official seeds and catalogue rows that are not in the registry yet."""
    data = data if data is not None else read_seed()
    todo = [(o.host + o.path, o.categories, TRUST_BASE_OFFICIAL, "official seed")
            for o in data.official]
    if catalogue is not None:
        todo += [(d + p, cats, TRUST_BASE_CATALOGUE, "catalogue import")
                 for d, p, cats in read_catalogue(catalogue)]
    n = 0
    for text, cats, base, reason in todo:
        if is_shared(parse_source(text)[1]):
            continue
        if await find(store, text) is None:
            await add(store, text, base, reason, "seed", cats, now_ms=now_ms)
            n += 1
    return n


# Nightly recompute: a pure function of its inputs.

@dataclass(frozen=True)
class TrustConfig:
    half_life_d: float
    prize_min: float
    contradict_min_owners: int
    penalties: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "TrustConfig":
        return cls(TRUST_HALF_LIFE_D, PRIZE_TRUST_MIN, TRUST_CONTRADICT_MIN_OWNERS,
                   dict(PENALTIES))


@dataclass(frozen=True)
class SourceState:
    source: SourceRow
    # Per-category base; other categories use source.base_trust.
    base: Dict[str, float]


def nightly(states: Sequence[SourceState], events: Sequence[SourceEventRow], now_ms: int,
            categories: Sequence[str], cfg: Optional[TrustConfig] = None) -> List[SourceTrustRow]:
    """SourceTrust rows for every (source, category): the base less decayed penalties."""
    cfg = cfg or TrustConfig.defaults()
    by_id = {s.source.id: s for s in states}

    def owner(sid: int) -> str:
        return by_id[sid].source.owner_id or f"source:{sid}"

    def base(sid: int, c: str) -> float:
        st = by_id[sid]
        return st.base.get(c, st.source.base_trust)

    def trusted(sid: Optional[int], c: str) -> bool:
        return sid in by_id and base(sid, c) >= cfg.prize_min

    def weight(at_ms: int) -> float:
        return 0.5 ** (max(0, now_ms - at_ms) / (cfg.half_life_d * DAY_MS))

    pens: Dict[Tuple[int, str], float] = defaultdict(float)
    cases: Dict[tuple, Tuple[set, int]] = {}
    for p in events:
        if (p.role not in PENALTIES or p.source_id not in by_id
                or p.at_ms < (by_id[p.source_id].source.reset_ms or 0)):
            continue
        key = (p.source_id, p.category)
        if p.role != CONTRADICT:
            pens[key] += cfg.penalties[p.role] * weight(p.at_ms)
        elif trusted(p.by_source_id, p.category) and owner(p.by_source_id) != owner(p.source_id):
            owners, at = cases.get((*key, p.event_id, p.claim_id), (set(), 0))
            cases[(*key, p.event_id, p.claim_id)] = (owners | {owner(p.by_source_id)},
                                                     max(at, p.at_ms))
    for (sid, c, _, _), (owners, at) in sorted(cases.items(), key=lambda x: str(x[0])):
        if len(owners) >= cfg.contradict_min_owners:
            pens[(sid, c)] += cfg.penalties[CONTRADICT] * weight(at)

    out = []
    for sid in sorted(by_id):
        st = by_id[sid]
        for c in sorted(set(categories) | set(st.base)):
            row = SourceTrustRow(sid, c, base(sid, c), penalties=round(pens.get((sid, c), 0.0), 6))
            out.append(replace(row, trust=_score(row, st.source.status)))
    return out
