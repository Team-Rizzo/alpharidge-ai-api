"""Shared adapter pieces: the feed client, item building and the probe claim."""

from __future__ import annotations

import asyncio
import email.utils
import logging
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from alpharidge_ai.schema import Actor, Claim, Fact, category_of
from alpharidge_ai.verify.fetch import Fetcher
from app.services.entities import EntityIndex, normalize_name
from app.services.store import AnswerKeyRow

AK_MATCH_H = 72
RELEASE_AHEAD_D = 7

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
# One code per indicator the adapters cover, with its usual long and short names. A code is
# the indicator's identity; names outside the table keep their folded name.
INDICATORS = {
    "cpi": ("Consumer Price Index", "CPI", "Consumer Price Index (CPI)"),
    "ppi": ("Producer Price Index", "PPI", "Producer Price Index (PPI)", "PPI final demand"),
    "payrolls": ("Payroll Employment", "nonfarm payrolls", "payrolls", "payroll employment",
                 "nonfarm payroll", "nonfarm payroll employment",
                 "total nonfarm payroll employment", "non-farm payrolls"),
    "unemployment_rate": ("Unemployment Rate", "unemployment", "jobless rate"),
    "average_hourly_earnings": ("Average Hourly Earnings",),
    "eci": ("Employment Cost Index", "ECI", "Employment Cost Index (ECI)"),
    "productivity": ("Productivity", "nonfarm productivity", "labor productivity"),
    "import_prices": ("Import Price Index", "U.S. Import Price Index", "import prices"),
    "export_prices": ("Export Price Index", "U.S. Export Price Index", "export prices"),
    "gdp": ("GDP", "gross domestic product", "real GDP", "real gross domestic product"),
    "pce_price_index": ("PCE price index", "PCE", "personal consumption expenditures price index"),
    "personal_income": ("personal income",),
    "trade_balance": ("trade balance", "trade deficit", "goods and services deficit",
                      "international trade in goods and services"),
}
_INDICATOR_CODES = {normalize_name(n): code for code, names in INDICATORS.items() for n in names}
FEED_ACCEPT = "application/rss+xml,application/atom+xml,application/xml,application/json," \
              "text/xml,text/calendar;q=0.9,*/*;q=0.5"


def feed_config(user_agent: Optional[str] = None):
    from app.services.verifier import fetch_config

    cfg = fetch_config()
    return replace(cfg, user_agent=user_agent) if user_agent else cfg


class FeedClient(Fetcher):
    """The shared fetcher's robots.txt, pacing and limits, for raw feed bytes."""

    async def get(self, url: str) -> Optional[bytes]:
        try:
            if not await self.allowed(url):
                logger.info(f"Answer key: robots.txt disallows {url}")
                return None
            await self._pace(urlsplit(url).netloc)
            return await asyncio.wait_for(self._read(url), self.cfg.timeout_s)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning(f"Answer key: fetch of {url} failed: {e!r}")
        except Exception as e:
            logger.warning(f"Answer key: fetch of {url} refused: {e!r}")
        return None

    async def _read(self, url: str) -> Optional[bytes]:
        async with self.client.stream("GET", url, headers={"Accept": FEED_ACCEPT}) as r:
            if r.status_code != 200:
                logger.warning(f"Answer key: {url} returned {r.status_code}")
                return None
            raw = bytearray()
            async for chunk in r.aiter_bytes():
                raw += chunk
                if len(raw) > self.cfg.max_bytes:
                    return None
        return bytes(raw)


class FeedAdapter:
    """One official feed. `poll` returns its current records; storing them is idempotent."""
    name = ""
    types: Sequence[str] = ()
    # Whether a record's published time is the official release time.
    release_times = True

    def __init__(self, client: Optional[FeedClient] = None):
        self._client = client

    def enabled(self) -> bool:
        return True

    def user_agent(self) -> Optional[str]:
        return None

    @property
    def client(self) -> FeedClient:
        if self._client is None:
            self._client = FeedClient(feed_config(self.user_agent()))
        return self._client

    async def poll(self, now_ms: int) -> List[AnswerKeyRow]:
        raise NotImplementedError

    def scheduled(self, now_ms: int) -> List[AnswerKeyRow]:
        """Upcoming releases from a calendar; used for release times only, never stored."""
        return []


def actor(name: str, id_scheme: str = "other", id: Optional[str] = None,
          ticker: Optional[str] = None) -> dict:
    out = {"id_scheme": id_scheme, "id": (id or ticker or name)[:64], "name": name[:200]}
    if ticker:
        out["ticker"] = ticker
    return out


def fact(field: str, value, unit: Optional[str] = None, alts: Iterable = (),
         exact: bool = False, labels: Optional[dict] = None) -> dict:
    """`labels`: claim fields and the values one of which a claim must name for this figure
    to be compared."""
    out = {"field": field, "value": value, "unit": unit}
    if alts:
        out["alts"] = list(alts)
    if exact:
        out["exact"] = True
    if labels:
        out["labels"] = {k: list(v) for k, v in labels.items()}
    return out


def item(adapter: str, external_id: str, event_type: str, event_date: date, title: str,
         url: str, actors: Sequence[dict], facts: Sequence[dict] = (),
         guards: Sequence[str] = (), stage: Optional[str] = None, hard_id: Optional[str] = None,
         published: Optional[datetime] = None) -> AnswerKeyRow:
    payload = {"title": title, "url": url, "actors": list(actors), "facts": list(facts),
               "guards": list(guards), "stage": stage, "hard_id": hard_id}
    return AnswerKeyRow(
        adapter=adapter, external_id=external_id[:200], payload=payload,
        category=category_of(event_type).value, event_type=event_type, event_date=event_date,
        published_ms=round(published.timestamp() * 1000) if published else None)


def indicator_code(name: Optional[str]) -> Optional[str]:
    return _INDICATOR_CODES.get(normalize_name(name))


def to_probe(row: AnswerKeyRow, entities: Optional[EntityIndex] = None) -> Claim:
    """The item as an event claim, so it goes through the same identity rules as miners'."""
    p = row.payload
    actors = []
    for a in p.get("actors", ()):
        rec = entities.by_ticker(a.get("ticker")) if entities is not None else None
        if rec is not None:
            actors.append(Actor(id_scheme=rec.scheme, id=rec.external_id, name=a["name"]))
        else:
            actors.append(Actor(id_scheme=a["id_scheme"], id=a["id"], name=a.get("name")))
    facts = [Fact(field=f["field"], value=f["value"], unit=f.get("unit"))
             for f in p.get("facts", ()) if f.get("value") not in (None, "")]
    return Claim(kind="event", headline=(p.get("title") or row.event_type)[:200],
                 event_type=row.event_type, category=row.category, stage=p.get("stage"),
                 actors=actors, event_date=row.event_date, facts=facts, source_url=p["url"],
                 hard_id=p.get("hard_id"))


def rss_date(text: Optional[str]) -> Optional[datetime]:
    try:
        dt = email.utils.parsedate_to_datetime((text or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def eastern_date(dt: datetime) -> date:
    return dt.astimezone(EASTERN).date()
