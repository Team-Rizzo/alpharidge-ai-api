"""Federal Reserve: FOMC statements from the monetary policy press release feed."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import List, Optional, Tuple

from app.services.answer_key.base import (
    AK_MATCH_H, FeedAdapter, FeedClient, actor, eastern_date, fact, item, rss_date,
)
from app.services.store import AnswerKeyRow
from app.utils.clock import HOUR_MS

AK_FED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"

logger = logging.getLogger(__name__)

NAME = "fed"
FED = actor("Federal Reserve", "wikidata", "Q53536")

_STATEMENT = re.compile(r"\bFOMC statement\b", re.I)
_NUM = r"\d+(?:-\d+/\d+)?|\d+/\d+"
_DECISION = re.compile(
    rf"\b(?P<verb>raise|increase|lower|reduce|maintain)\s+the\s+target\s+range\s+for\s+the\s+"
    rf"federal\s+funds\s+rate(?:\s+by\s+(?P<by>{_NUM})\s+percentage\s+point)?\s+(?:to|at)\s+"
    rf"(?P<lo>{_NUM})\s+to\s+(?P<hi>{_NUM})\s+percent", re.I)
_VOTE = re.compile(r"\bby an? (\d+)\s*[-–—]\s*(\d+) vote\b", re.I)


def number(text: str) -> float:
    """'3-3/4' as 3.75, '1/4' as 0.25."""
    whole, _, frac = text.partition("-")
    if "/" in whole:
        return float(Fraction(whole))
    return float(int(whole) + (Fraction(frac) if frac else 0))


def statement_facts(text: str) -> List[dict]:
    m = _DECISION.search(" ".join((text or "").split()))
    if m is None:
        return []
    lo, hi = number(m.group("lo")), number(m.group("hi"))
    sign = {"raise": 1, "increase": 1, "lower": -1, "reduce": -1}.get(m.group("verb").lower(), 0)
    change = round(sign * number(m.group("by")) * 100) if m.group("by") and sign else 0
    facts = [fact("new_rate", hi, "percent", alts=[lo]),
             fact("change_bps", float(change), "basis_points")]
    vote = _VOTE.search(text)
    if vote:
        facts.append(fact("vote_split", f"{vote.group(1)}-{vote.group(2)}"))
    return facts


def parse(raw: bytes) -> List[Tuple[str, str, object]]:
    """(title, link, published) for each FOMC statement in the feed."""
    out = []
    for node in ET.fromstring(raw).iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        published = rss_date(node.findtext("pubDate"))
        if _STATEMENT.search(title) and link.startswith("http") and published:
            out.append((title, link, published))
    return out


class Fed(FeedAdapter):
    name = NAME
    types = ("monetary_policy",)

    def __init__(self, client: Optional[FeedClient] = None):
        super().__init__(client)
        self._done: set = set()

    async def poll(self, now_ms: int) -> List[AnswerKeyRow]:
        raw = await self.client.get(AK_FED_URL)
        if raw is None:
            return []
        try:
            entries = parse(raw)
        except ET.ParseError as e:
            logger.warning(f"Answer key: Fed feed parse failed: {e!r}")
            return []
        cutoff = now_ms - int(AK_MATCH_H * HOUR_MS)
        out = []
        for title, link, published in entries:
            if published.timestamp() * 1000 < cutoff or link in self._done:
                continue
            page = await self.client.fetch(link)
            facts = statement_facts(page.text) if page.ok else []
            if not facts:
                # Try again on the next poll rather than store a record without the decision.
                continue
            self._done.add(link)
            out.append(item(NAME, link, "monetary_policy", eastern_date(published),
                            "Federal Reserve FOMC decision", link, [FED], facts,
                            published=published))
        return out
