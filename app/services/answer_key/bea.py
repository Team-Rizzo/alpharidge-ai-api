"""BEA: release times of the headline releases (GDP, personal income, trade) from the public
schedule, for the release rule only. It stores no records.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.services.answer_key.base import (
    AK_MATCH_H, EASTERN, RELEASE_AHEAD_D, FeedAdapter, actor, fact, item,
)
from app.services.store import AnswerKeyRow
from app.utils.clock import HOUR_MS

AK_BEA_URL = "https://www.bea.gov/news/schedule"

logger = logging.getLogger(__name__)

NAME = "bea"
BEA = actor("Bureau of Economic Analysis", "wikidata", "Q1443033")

# Release title pattern -> (indicator, aliases).
RELEASES: Tuple[Tuple[re.Pattern, str, Tuple[str, ...]], ...] = (
    (re.compile(r"^GDP \("), "GDP", ("gdp", "gross domestic product", "real gdp")),
    (re.compile(r"^Personal Income and Outlays"), "Personal income and outlays",
     ("personal income", "personal income and outlays", "pce", "pce price index")),
    (re.compile(r"^U\.S\. International Trade in Goods and Services"), "Trade balance",
     ("trade balance", "trade deficit", "goods and services deficit")),
)

_YEAR = re.compile(r">\s*Year (\d{4})\s*<")
_ROW = re.compile(r'<div class="release-date">\s*(?P<day>[A-Z][a-z]+ \d{1,2})\s*</div>\s*'
                  r'<small[^>]*>\s*(?P<time>\d{1,2}:\d{2} [AP]M)\s*</small>.*?'
                  r'class="release-title[^"]*"[^>]*>(?P<title>[^<]+)<', re.S)


def _release(title: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    for pattern, indicator, names in RELEASES:
        if pattern.search(title):
            return indicator, names
    return None


def schedule(raw: bytes) -> List[Tuple[str, datetime]]:
    """(title, release time) for each listed release."""
    text = raw.decode("utf-8", errors="replace")
    years = [(m.start(), int(m.group(1))) for m in _YEAR.finditer(text)]
    out = []
    for m in _ROW.finditer(text):
        year = next((y for at, y in reversed(years) if at < m.start()), None)
        if year is None:
            continue
        try:
            at = datetime.strptime(f"{m.group('day')} {year} {m.group('time')}",
                                   "%B %d %Y %I:%M %p")
        except ValueError:
            continue
        title = " ".join(html.unescape(m.group("title")).split())
        out.append((title, at.replace(tzinfo=EASTERN)))
    return out


def records(entries: List[Tuple[str, datetime]], url: str) -> List[AnswerKeyRow]:
    out = []
    for title, at in entries:
        found = _release(title)
        if found is None:
            continue
        indicator, names = found
        out.append(item(NAME, f"{indicator}|{title}", "economic_data", at.date(),
                        f"BEA {title}", url, [BEA],
                        [fact("indicator", indicator, alts=names, exact=True)],
                        guards=["indicator"], published=at))
    return out


class Bea(FeedAdapter):
    name = NAME
    types = ("economic_data",)

    def __init__(self, client=None):
        super().__init__(client)
        self._upcoming: Dict[str, datetime] = {}

    async def poll(self, now_ms: int) -> List[AnswerKeyRow]:
        raw = await self.client.get(AK_BEA_URL)
        if raw is not None:
            self._upcoming.update(schedule(raw))
        cutoff = now_ms - int(AK_MATCH_H * HOUR_MS)
        for t, at in list(self._upcoming.items()):
            if at.timestamp() * 1000 < cutoff:
                del self._upcoming[t]
        return []

    def scheduled(self, now_ms: int) -> List[AnswerKeyRow]:
        until = now_ms + int(RELEASE_AHEAD_D * 24 * HOUR_MS)
        return records([(t, at) for t, at in self._upcoming.items()
                        if at.timestamp() * 1000 <= until], AK_BEA_URL)
