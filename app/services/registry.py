"""Registry IDs: resolve a miner's Wikidata, SEC or GLEIF ID once, vet it and cache it."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Protocol, Tuple
from urllib.parse import urlsplit

import httpx

from alpharidge_ai.schema import Actor, IdScheme
from alpharidge_ai.verify.filing import submissions_url
from app.config import EVENT_LANE
from app.services.entities import (
    LINKED, UNCLEAR, EntityIndex, EntityRecord, canonical_id, id_key, normalize_name,
)
from app.utils.clock import DAY_MS

REGISTRY_TIMEOUT_S = 10
REGISTRY_MIN_INTERVAL_S = 1.0
REGISTRY_MIN_AGE_D = 90
REGISTRY_PERSON_SITELINKS = 1
REGISTRY_PER_TICK = 20
# Lookups run beside the worker; the tick waits this long for each new one.
REGISTRY_IN_FLIGHT = 8
REGISTRY_INLINE_WAIT_S = 0.2
REGISTRY_RETRY_S = 60
REGISTRY_RETRY_MAX_S = 3600
REGISTRY_MISS_TTL_S = 86400
REGISTRY_UNCLEAR_TTL_S = 86400
# Longest a claim waits on a lookup; keep it below the check budget window.
REGISTRY_MAX_HOLD_S = 6 * 3600
REGISTRY_ORG_SITELINKS = 10
# Wikidata classes (P31) by kind. An item of several kinds takes the first in this order.
REGISTRY_KIND_CLASSES = {
    "central_bank": ("Q66344",),
    "statistics_agency": ("Q480242",),
    "exchange": ("Q11691",),
    "party": ("Q7278", "Q2023214", "Q6738353"),
    "regulator": (
        "Q327333", "Q1639780", "Q105062392", "Q1465684", "Q20857065", "Q1752939",
        "Q3044788", "Q20858006"),
    "government": (
        "Q6256", "Q3624078", "Q7188", "Q1006644", "Q35798", "Q1802419", "Q1320217",
        "Q192350", "Q910252", "Q2500378", "Q15711797", "Q1243341", "Q1241288", "Q6867185",
        "Q15409298", "Q4294273", "Q1335818", "Q120121699", "Q35657", "Q107390", "Q1221156",
        "Q11828004", "Q10742", "Q779415", "Q133442", "Q515", "Q1093829", "Q50337",
        "Q124313007"),
    "legislature": (
        "Q11204", "Q35749", "Q189445", "Q37002670", "Q375928", "Q637846", "Q110315658",
        "Q110316962", "Q7603708", "Q3270264", "Q2570643", "Q2145277", "Q9247597",
        "Q110315866", "Q2282872", "Q97374157", "Q2532278", "Q3154693"),
    "league": (
        "Q623109", "Q15991290", "Q15991303", "Q18536323", "Q6631808", "Q75179296",
        "Q124059801", "Q60147807", "Q50301185", "Q59134402"),
    "team": (
        "Q476028", "Q13393265", "Q17156793", "Q13027888", "Q4498974", "Q12973014", "Q847017",
        "Q15944511", "Q10497835", "Q17376093", "Q63171169", "Q6979593", "Q135408445",
        "Q23905105", "Q58840819", "Q86255944"),
    "federation": ("Q1478443", "Q11422536", "Q58973026"),
    "award": ("Q618779", "Q15229207", "Q652965", "Q9639292", "Q23719064", "Q107655869"),
    "company": (
        "Q4830453", "Q891723", "Q6881511", "Q783794", "Q658255", "Q22687", "Q46970",
        "Q167037", "Q1589009", "Q161726", "Q129238", "Q1058914", "Q18388277", "Q210167",
        "Q1331793", "Q219577", "Q778575", "Q5225895", "Q6832945", "Q21980538", "Q1194970",
        "Q848507", "Q60767305", "Q786820", "Q187939"),
    "research": ("Q3918", "Q875538", "Q902104", "Q15936437", "Q31855"),
}
REGISTRY_P31_KINDS = {c: k for k, cs in REGISTRY_KIND_CLASSES.items() for c in cs}
_KIND_RANK = {k: n for n, k in enumerate(REGISTRY_KIND_CLASSES)}
# Robots-allowed reads: an item's history page for the revision, then that revision's data.
REGISTRY_WIKIDATA_HISTORY = "https://www.wikidata.org/wiki/{id}"
REGISTRY_WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{id}.json"
REGISTRY_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records/{id}"

logger = logging.getLogger(__name__)

WIKIDATA, CIK, LEI = IdScheme.WIKIDATA.value, IdScheme.CIK.value, IdScheme.LEI.value
HUMAN = "Q5"
_QID = re.compile(r"Q[1-9][0-9]{0,11}")
_REVID = re.compile(r'data-mw-revid="([0-9]+)"')
_PROPS = {"cik": "P5531", "lei": "P1278", "website": "P856", "classes": "P31",
          "offices": "P39", "teams": "P54"}


class RegistryUnavailable(Exception):
    """The registry could not answer now; try again later."""


@dataclass(frozen=True)
class RegistryRecord:
    """A registry's answer for one ID."""
    scheme: str
    external_id: str
    name: str
    aliases: Tuple[str, ...] = ()
    established: bool = False
    active: bool = True
    classes: Tuple[str, ...] = ()
    xrefs: Tuple[str, ...] = ()
    website: Optional[str] = None
    offices: Tuple[str, ...] = ()
    teams: Tuple[str, ...] = ()
    sitelinks: int = 0
    tickers: Tuple[str, ...] = ()
    revision: Optional[str] = None
    redirected_from: Optional[str] = None

    @property
    def names(self) -> set:
        return {n for n in map(normalize_name, (self.name, *self.aliases, *self.tickers)) if n}


class RegistrySource(Protocol):
    async def fetch(self, scheme: str, external_id: str,
                    before_ms: int) -> Optional[RegistryRecord]: ...


class FixtureResolver:
    """Saved answers by (scheme, id); an exception value is raised."""

    def __init__(self, records: Iterable = ()):
        self.records: Dict[str, object] = {}
        self.calls: List[str] = []
        for r in records:
            self.add(r)

    def add(self, record, key: Optional[str] = None) -> None:
        self.records[key or id_key(record.scheme, record.external_id)] = record

    async def fetch(self, scheme, external_id, before_ms):
        key = id_key(scheme, external_id)
        self.calls.append(key)
        hit = self.records.get(key)
        if isinstance(hit, Exception):
            raise hit
        return hit


def _ms(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)


def _host(url: Optional[str]) -> str:
    host = urlsplit(url if "//" in (url or "") else f"//{url or ''}").hostname or ""
    return host[4:] if host.startswith("www.") else host


def _same_site(a: Optional[str], b: Optional[str]) -> bool:
    return bool(_host(a)) and _host(a) == _host(b)


def _values(claims: dict, prop: str) -> Tuple[str, ...]:
    out = []
    for st in claims.get(prop, ()):
        if st.get("rank") == "deprecated":
            continue
        v = (st.get("mainsnak") or {}).get("datavalue", {}).get("value")
        v = v.get("id") if isinstance(v, dict) else v
        if isinstance(v, str) and v:
            out.append(v)
    return tuple(out)


class HttpResolver:
    """Wikidata, SEC and GLEIF over HTTP, one request per host per interval."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self.client = client or httpx.AsyncClient(timeout=REGISTRY_TIMEOUT_S,
                                                  follow_redirects=True)
        self._next: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _agent(self, scheme: str) -> str:
        if scheme == CIK:
            return EVENT_LANE["AK_SEC_USER_AGENT"]
        return EVENT_LANE["USER_AGENT"]

    async def _pace(self, host: str) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next.get(host, 0.0))
            self._next[host] = at + REGISTRY_MIN_INTERVAL_S
        if at > now:
            await asyncio.sleep(at - now)

    async def _get(self, url: str, scheme: str, params: Optional[dict] = None,
                   text: bool = False):
        await self._pace(urlsplit(url).netloc)
        try:
            r = await self.client.get(url, params=params, headers={
                "User-Agent": self._agent(scheme),
                "Accept": "text/html" if text else "application/json"})
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise RegistryUnavailable(repr(e)) from e
        if r.status_code == 404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            raise RegistryUnavailable(f"{url} returned {r.status_code}")
        if r.status_code != 200:
            return None
        if text:
            return r.text
        try:
            return r.json()
        except ValueError as e:
            raise RegistryUnavailable(f"{url} returned bad JSON") from e

    async def fetch(self, scheme, external_id, before_ms):
        ext = canonical_id(scheme, external_id)
        if scheme == WIKIDATA:
            return await self._wikidata(ext, before_ms)
        if scheme == CIK and EVENT_LANE["AK_SEC_USER_AGENT"] and ext.isdigit():
            return await self._sec(ext, before_ms)
        if scheme == LEI:
            return await self._gleif(ext, before_ms)
        return None

    async def _wikidata(self, qid: str, before_ms: int) -> Optional[RegistryRecord]:
        if not _QID.fullmatch(qid):
            return None
        stamp = datetime.fromtimestamp(before_ms / 1000, tz=timezone.utc)
        page = await self._get(REGISTRY_WIKIDATA_HISTORY.format(id=qid), WIKIDATA, {
            "action": "history", "offset": stamp.strftime("%Y%m%d%H%M%S"), "limit": "1"},
            text=True)
        if page is None:
            return None
        rev = _REVID.search(page)
        if rev is None:
            return RegistryRecord(WIKIDATA, qid, qid)
        data = await self._get(REGISTRY_WIKIDATA_ENTITY.format(id=qid), WIKIDATA,
                               {"revision": rev.group(1)})
        ents = (data or {}).get("entities") or {}
        if len(ents) != 1:
            return None
        target, ent = next(iter(ents.items()))
        target = str(target).upper()
        moved = qid if target != qid else None
        rev = rev.group(1)
        claims = ent.get("claims") or {}
        label = ((ent.get("labels") or {}).get("en") or {}).get("value") or target
        aliases = tuple(a["value"] for a in (ent.get("aliases") or {}).get("en", ())
                        if a.get("value"))
        sites = _values(claims, _PROPS["website"])
        xrefs = (*(f"cik:{v}" for v in _values(claims, _PROPS["cik"])),
                 *(f"lei:{v}" for v in _values(claims, _PROPS["lei"])))
        return RegistryRecord(
            WIKIDATA, target, label, aliases, established=True,
            classes=_values(claims, _PROPS["classes"]), xrefs=xrefs,
            website=sites[0] if sites else None, offices=_values(claims, _PROPS["offices"]),
            teams=_values(claims, _PROPS["teams"]), sitelinks=len(ent.get("sitelinks") or {}),
            revision=rev, redirected_from=moved)

    async def _sec(self, cik: str, before_ms: int) -> Optional[RegistryRecord]:
        data = await self._get(submissions_url(cik), CIK)
        if not data or not data.get("name"):
            return None
        filings = data.get("filings") or {}
        dates = [_ms(d) for d in (filings.get("recent") or {}).get("filingDate", ())]
        oldest = min((d for d in dates if d is not None), default=None)
        old = bool(filings.get("files")) or (oldest is not None and oldest < before_ms)
        return RegistryRecord(
            CIK, cik, data["name"], tuple(f["name"] for f in data.get("formerNames") or ()
                                          if f.get("name")),
            established=old, website=data.get("website") or None,
            tickers=tuple(t for t in data.get("tickers") or () if t))

    async def _gleif(self, lei: str, before_ms: int) -> Optional[RegistryRecord]:
        data = await self._get(REGISTRY_GLEIF_URL.format(id=lei), LEI)
        attrs = ((data or {}).get("data") or {}).get("attributes") or {}
        entity = attrs.get("entity") or {}
        name = (entity.get("legalName") or {}).get("name")
        if not name:
            return None
        since = _ms((attrs.get("registration") or {}).get("initialRegistrationDate"))
        return RegistryRecord(
            LEI, lei, name, tuple(n["name"] for n in entity.get("otherNames") or ()
                                  if n.get("name")),
            established=since is not None and since < before_ms,
            active=entity.get("status") == "ACTIVE")


class Registry:
    """Resolves unseen actor IDs into the entity index, holding claims while it can't."""

    def __init__(self, resolver: Optional[RegistrySource] = None):
        self.resolver = resolver
        self._retry: Dict[str, Tuple[int, int]] = {}
        self._misses: Dict[str, int] = {}
        self._tick = (0, 0)
        self._warmed: Optional[int] = None
        self._pending: Dict[str, asyncio.Task] = {}

    async def warm(self, store, index: EntityIndex) -> None:
        """Adds the cached registry records to the index, once per index."""
        if self._warmed == id(index):
            return
        for r in await store.registry_entities():
            index.add(r)
        self._warmed = id(index)

    def _ids(self, actors: Iterable[Actor]) -> List[Tuple[str, str]]:
        return [(IdScheme(a.id_scheme).value, a.id) for a in actors
                if IdScheme(a.id_scheme) in (IdScheme.WIKIDATA, IdScheme.CIK, IdScheme.LEI)]

    def holding(self, received_ms: int, now_ms: int) -> bool:
        """True while a claim received then may still be waiting on a lookup."""
        hold = int(REGISTRY_MAX_HOLD_S * 1000)
        return self.resolver is not None and now_ms - received_ms < hold

    def _missed(self, key: str, now_ms: int) -> bool:
        ttl = int(REGISTRY_MISS_TTL_S * 1000)
        return now_ms - self._misses.get(key, -ttl) < ttl

    def _stale(self, record: EntityRecord, now_ms: int) -> bool:
        ttl = int(REGISTRY_UNCLEAR_TTL_S * 1000)
        return (record.origin == "registry" and record.status == UNCLEAR
                and now_ms - (record.checked_ms or 0) >= ttl)

    def _spend(self, now_ms: int) -> bool:
        at, used = self._tick
        if at != now_ms:
            at, used = now_ms, 0
        if used >= REGISTRY_PER_TICK:
            return False
        self._tick = (at, used + 1)
        return True

    async def ensure(self, store, index: EntityIndex, actors: Iterable[Actor],
                     now_ms: int, received_ms: Optional[int] = None) -> bool:
        """True once every registry ID of these actors is settled; False to hold the claim.

        Linked records stay as first cached; unclear ones are looked up again after a while.
        An ID still unanswered after the hold limit is treated as not found."""
        if self.resolver is None:
            return True
        for scheme, ext in self._ids(actors):
            key = id_key(scheme, ext)
            known = index.get(scheme, ext)
            if (known is not None and not self._stale(known, now_ms)) or self._missed(key, now_ms):
                continue
            if known is None and received_ms is not None and not self.holding(received_ms, now_ms):
                continue
            task = self._pending.get(key)
            if task is None:
                tries, at = self._retry.get(key, (0, 0))
                if (now_ms < at or len(self._pending) >= REGISTRY_IN_FLIGHT
                        or not self._spend(now_ms)):
                    if known is None:
                        return False
                    continue
                task = self._pending[key] = asyncio.create_task(
                    self._lookup(store, index, scheme, ext, now_ms, tries))
                task.add_done_callback(lambda _t, k=key: self._pending.pop(k, None))
                # A quick answer settles the claim this tick; a slow one holds it.
                await asyncio.wait({task}, timeout=REGISTRY_INLINE_WAIT_S)
            if not task.done() or not task.result():
                if known is None:
                    return False
                continue
        return True

    async def _lookup(self, store, index: EntityIndex, scheme: str, ext: str, now_ms: int,
                      tries: int) -> bool:
        """True once the ID is answered, found or not."""
        key = id_key(scheme, ext)
        try:
            rec = await self.resolve(scheme, ext, now_ms, index)
            if rec is not None:
                await store.put_entity(rec)
        except Exception as e:
            wait = min(REGISTRY_RETRY_S * 2 ** tries, REGISTRY_RETRY_MAX_S)
            self._retry[key] = (tries + 1, now_ms + int(wait * 1000))
            logger.info(f"Registry lookup of {key} deferred: {e!r}")
            return False
        self._retry.pop(key, None)
        if rec is None:
            self._misses[key] = now_ms
            return True
        index.add(rec)
        return True

    async def resolve(self, scheme: str, ext: str, now_ms: int,
                      index: Optional[EntityIndex] = None) -> Optional[EntityRecord]:
        before = now_ms - int(REGISTRY_MIN_AGE_D * DAY_MS)
        raw = await self.resolver.fetch(scheme, ext, before)
        if raw is None:
            return None
        return replace(await self.vet(raw, before, index), checked_ms=now_ms)

    async def vet(self, raw: RegistryRecord, before_ms: int,
                  index: Optional[EntityIndex] = None) -> EntityRecord:
        """The record as cached. A CIK or LEI anchors one entity only."""
        tickers = tuple(f"ticker:{t}" for t in raw.tickers)
        if raw.scheme != WIKIDATA:
            ok = raw.established and raw.active
            return self._record(raw, "company", tickers, ok,
                                website=raw.website if raw.scheme == CIK else None)
        moved = (id_key(WIKIDATA, raw.redirected_from),) if raw.redirected_from else ()
        mine = {id_key(WIKIDATA, raw.external_id), *moved}
        anchors, sites = [], []
        for x in raw.xrefs:
            scheme, ext = x.split(":", 1)
            key = id_key(scheme, ext)
            owner = index.get(scheme, ext) if index is not None else None
            if owner is not None and owner.key not in (key, *mine):
                continue
            other = await self.resolver.fetch(scheme, ext, before_ms)
            if (other is not None and other.established and other.active
                    and raw.names & other.names):
                anchors.append(key)
                if other.website:
                    sites.append(other.website)
        website = raw.website if anchors and all(_same_site(raw.website, s) for s in sites) \
            else None
        if HUMAN in raw.classes:
            roles = (*(f"office:{q}" for q in raw.offices), *(f"team:{q}" for q in raw.teams))
            # Without a public role the person links as "person" and stays labelled unverified.
            kind = "official" if raw.offices else "athlete" if raw.teams else "person"
            ok = raw.established and raw.sitelinks >= REGISTRY_PERSON_SITELINKS
            return self._record(raw, kind, (*moved, *roles), ok)
        kind = min((REGISTRY_P31_KINDS[c] for c in raw.classes if c in REGISTRY_P31_KINDS),
                   key=_KIND_RANK.get, default=None)
        ok = raw.established and kind is not None and bool(
            anchors or raw.website or raw.sitelinks >= REGISTRY_ORG_SITELINKS)
        return self._record(raw, kind, (*moved, *anchors), ok, website=website)

    @staticmethod
    def _record(raw: RegistryRecord, kind: Optional[str], xrefs: Tuple[str, ...], ok: bool,
                website: Optional[str] = None) -> EntityRecord:
        return EntityRecord(raw.scheme, canonical_id(raw.scheme, raw.external_id), raw.name,
                            kind, raw.aliases, xrefs, website, status=LINKED if ok else UNCLEAR,
                            origin="registry", revision=raw.revision)


_default: Optional[Registry] = None


def get_registry() -> Registry:
    global _default
    if _default is None:
        mode = EVENT_LANE["REGISTRY"]
        if mode == "http" and "(+" not in EVENT_LANE["USER_AGENT"]:
            logger.warning("Registry lookups are off: EL_USER_AGENT_CONTACT is not set")
            mode = "off"
        _default = Registry(HttpResolver() if mode == "http" else None)
    return _default
