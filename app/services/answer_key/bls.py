"""BLS: the latest-numbers feed for values, dated by the BLS release calendar."""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.config import EVENT_LANE
from app.services.answer_key.base import (
    AK_MATCH_H, EASTERN, RELEASE_AHEAD_D, FeedAdapter, actor, fact, item,
)
from app.services.store import AnswerKeyRow
from app.utils.clock import HOUR_MS

AK_CALENDAR_TTL_S = 86400

logger = logging.getLogger(__name__)

NAME = "bls"
BLS = actor("Bureau of Labor Statistics", "wikidata", "Q2928428")

# News release slug -> its name in the release calendar.
RELEASES = {
    "cpi": "Consumer Price Index",
    "empsit": "Employment Situation",
    "ppi": "Producer Price Index",
    "eci": "Employment Cost Index",
    "prod": "Productivity and Costs",
    "ximpim": "U.S. Import and Export Price Indexes",
}
# The change the feed's percent figures give, per release: a claimed percent figure is
# compared only when it names the same `measure`.
MEASURES = {"cpi": "mom", "ppi": "mom", "ximpim": "mom", "eci": "qoq", "prod": "qoq"}
ALIASES = {
    "Payroll Employment": ("nonfarm payrolls", "payrolls", "payroll employment",
                           "nonfarm payroll employment"),
    "Unemployment Rate": ("unemployment", "unemployment rate"),
}

_BLOCK = re.compile(r"<p>\s*(?P<name>[^<:]+):.*?class=\"data\">(?P<data>[^<]+)<.*?"
                    r"news\.release/(?P<slug>[a-z_]+)\.toc\.htm", re.S)
_VALUE = re.compile(r"(?P<sign>[+-]?)(?P<cur>\$?)(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<pct>%?)")
_PAREN = re.compile(r"\(([^)]+)\)")


def aliases(name: str) -> List[str]:
    base = _PAREN.sub("", name).replace(" - ", " ").strip().lower()
    out = [name.lower(), base, *(m.lower() for m in _PAREN.findall(name))]
    return list(dict.fromkeys([*out, *ALIASES.get(name, ())]))


def value(text: str) -> Optional[Tuple[float, str]]:
    m = _VALUE.search(text)
    if m is None:
        return None
    v = float(m.group("num").replace(",", "")) * (-1 if m.group("sign") == "-" else 1)
    return v, "percent" if m.group("pct") else "usd" if m.group("cur") else "count"


def calendar(raw: bytes) -> Dict[str, List[datetime]]:
    """Release name -> release times, from the iCalendar schedule."""
    out: Dict[str, List[datetime]] = {}
    summary, start = None, None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            summary, start = None, None
        elif line.startswith("SUMMARY:"):
            summary = line[8:].replace("\\,", ",").strip()
        elif line.startswith("DTSTART"):
            try:
                start = datetime.strptime(line.split(":", 1)[1][:15], "%Y%m%dT%H%M%S")
            except ValueError:
                start = None
        elif line == "END:VEVENT" and summary and start:
            out.setdefault(summary, []).append(start.replace(tzinfo=EASTERN))
    return out


def parse(raw: bytes, releases: Dict[str, List[datetime]], now: datetime) -> List[AnswerKeyRow]:
    out = []
    for node in ET.fromstring(raw).iter("item"):
        body = node.findtext("description") or ""
        for m in _BLOCK.finditer(body):
            name = html.unescape(m.group("name")).strip()
            data = html.unescape(m.group("data")).strip()
            release = RELEASES.get(m.group("slug"))
            got = value(data)
            if release is None or got is None:
                continue
            past = [t for t in releases.get(release, ()) if t <= now]
            if not past:
                continue
            at = max(past)
            period = data.split(" in ", 1)[1].strip() if " in " in data else ""
            names = aliases(name)
            measure = MEASURES.get(m.group("slug")) if got[1] == "percent" else None
            facts = [fact("indicator", name, alts=names, exact=True),
                     fact("actual", got[0], got[1],
                          labels={"measure": [measure]} if measure else None)]
            if period:
                facts.append(fact("period", period))
            url = f"https://www.bls.gov/news.release/{m.group('slug')}.toc.htm"
            out.append(item(NAME, f"{m.group('slug')}|{name}|{period}", "economic_data",
                            at.date(), f"BLS {name}", url, [BLS], facts, guards=["indicator"],
                            published=at))
    return out


def scheduled(releases: Dict[str, List[datetime]], since_ms: int,
              until_ms: int) -> List[AnswerKeyRow]:
    out = []
    for slug, name in RELEASES.items():
        for at in releases.get(name, ()):
            if since_ms <= at.timestamp() * 1000 <= until_ms:
                out.append(item(NAME, f"{slug}|{at.isoformat()}", "economic_data", at.date(),
                                f"BLS {name}", f"https://www.bls.gov/news.release/{slug}.toc.htm",
                                [BLS], published=at))
    return out


class Bls(FeedAdapter):
    name = NAME
    types = ("economic_data",)

    def __init__(self, client=None):
        super().__init__(client)
        self._calendar: Tuple[float, Dict[str, List[datetime]]] = (0.0, {})

    async def _releases(self) -> Dict[str, List[datetime]]:
        expires, cached = self._calendar
        if time.monotonic() < expires and cached:
            return cached
        raw = await self.client.get(EVENT_LANE["AK_BLS_CALENDAR_URL"])
        if raw is not None:
            cached = calendar(raw)
            self._calendar = (time.monotonic() + AK_CALENDAR_TTL_S, cached)
        return cached

    def scheduled(self, now_ms: int) -> List[AnswerKeyRow]:
        return scheduled(self._calendar[1], now_ms - int(AK_MATCH_H * HOUR_MS),
                         now_ms + int(RELEASE_AHEAD_D * 24 * HOUR_MS))

    async def poll(self, now_ms: int) -> List[AnswerKeyRow]:
        releases = await self._releases()
        raw = await self.client.get(EVENT_LANE["AK_BLS_URL"]) if releases else None
        if raw is None:
            return []
        try:
            return parse(raw, releases, datetime.fromtimestamp(now_ms / 1000, tz=EASTERN))
        except ET.ParseError as e:
            logger.warning(f"Answer key: BLS feed parse failed: {e!r}")
            return []
