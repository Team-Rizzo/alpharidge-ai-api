"""SEC EDGAR: recent 8-K, S-1 and Schedule 13D/G filings from the public search feed."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from alpharidge_ai.schema import TYPE_SPEC, EventType
from app.config import EVENT_LANE
from app.services.answer_key.base import EASTERN, FeedAdapter, actor, fact, item
from app.services.answer_key.releases import NO_SCOOP_TYPES, RELEASE_BOOK_MAX
from app.services.store import AnswerKeyRow

AK_EDGAR_URL = ("https://efts.sec.gov/LATEST/search-index?q=&dateRange=custom&forms={forms}"
                "&startdt={start}&enddt={end}&from={offset}")
AK_EDGAR_FORMS = ("8-K", "S-1", "SC 13D,SCHEDULE 13D", "SC 13G,SCHEDULE 13G")
AK_EDGAR_PAGES = 2
AK_EDGAR_PAGE_SIZE = 100
# Filing index pages read per poll for acceptance times.
AK_EDGAR_ACCEPT_MAX = 20

logger = logging.getLogger(__name__)

NAME = "edgar"

# 8-K items with a matching event type: (type, stage).
ITEMS: Dict[str, Tuple[str, Optional[str]]] = {
    "1.03": ("bankruptcy", None),
    "1.05": ("security_incident", None),
    "2.01": ("merger_acquisition", "completed"),
    "2.02": ("earnings", None),
    "2.05": ("layoffs", None),
    "3.01": ("delisting", None),
    "5.02": ("personnel", None),
}
FORMS: Dict[str, Tuple[str, Optional[str]]] = {
    "S-1": ("ipo_listing", "announced"),
    "SC 13D": ("merger_acquisition", None),
    "SCHEDULE 13D": ("merger_acquisition", None),
    "SC 13G": ("merger_acquisition", None),
    "SCHEDULE 13G": ("merger_acquisition", None),
}

_ACCEPTED = re.compile(r'class="infoHead">\s*Accepted\s*</div>\s*<div class="info">\s*'
                       r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
_DISPLAY = re.compile(
    r"^(?P<name>.*?)\s*(?:\((?P<tickers>[A-Z0-9.,\s-]+)\))?\s*\(CIK (?P<cik>\d+)\)\s*$")


def _actors(names) -> Tuple[List[dict], List[str]]:
    actors, tickers = [], []
    for raw in names or ():
        m = _DISPLAY.match(str(raw).strip())
        if m is None:
            continue
        ticks = [t.strip() for t in (m.group("tickers") or "").split(",") if t.strip()]
        tickers.extend(ticks)
        actors.append(actor(m.group("name").strip(), "cik", m.group("cik").zfill(10),
                            ticks[0] if ticks else None))
    return actors, tickers


def _url(cik: str, adsh: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/"
            f"{adsh}-index.htm")


def parse(raw: bytes) -> List[AnswerKeyRow]:
    data = json.loads(raw)
    out: List[AnswerKeyRow] = []
    seen = set()
    for hit in (data.get("hits") or {}).get("hits") or ():
        src = hit.get("_source") or {}
        adsh = src.get("adsh") or str(hit.get("_id", "")).split(":", 1)[0]
        form = str(src.get("form") or src.get("file_type") or "").upper().strip()
        if not adsh or adsh in seen or form.endswith("/A"):
            continue
        seen.add(adsh)
        actors, tickers = _actors(src.get("display_names"))
        try:
            filed = date.fromisoformat(str(src.get("file_date"))[:10])
        except ValueError:
            continue
        if not actors:
            continue
        if form == "8-K":
            kinds = [(f"{adsh}#{i}", *ITEMS[i]) for i in src.get("items") or () if i in ITEMS]
        elif form in FORMS:
            kinds = [(adsh, *FORMS[form])]
        else:
            kinds = []
        for external_id, event_type, stage in kinds:
            facts = []
            if tickers and "ticker" in TYPE_SPEC[EventType(event_type)].slots:
                facts.append(fact("ticker", tickers[0], exact=True))
            out.append(item(NAME, external_id, event_type, filed,
                            f"{form} {actors[0]['name']}", _url(actors[0]["id"], adsh), actors,
                            facts, stage=stage, hard_id=f"edgar:{adsh}"))
    return out


def accepted(raw: bytes) -> Optional[datetime]:
    """The acceptance time on a filing index page."""
    m = _ACCEPTED.search(raw.decode("utf-8", errors="replace"))
    if m is None:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)


class Edgar(FeedAdapter):
    name = NAME
    types = tuple(sorted({t for t, _ in (*ITEMS.values(), *FORMS.values())}))

    def __init__(self, client=None):
        super().__init__(client)
        self._accepted: Dict[str, Optional[int]] = {}

    async def _acceptance(self, rows: List[AnswerKeyRow]) -> None:
        """Acceptance times for the filings of scheduled types, a few index pages per poll."""
        reads = 0
        for r in rows:
            if r.event_type not in NO_SCOOP_TYPES:
                continue
            url = r.payload["url"]
            if url not in self._accepted:
                if reads >= AK_EDGAR_ACCEPT_MAX:
                    continue
                reads += 1
                raw = await self.client.get(url)
                if raw is None:
                    continue
                at = accepted(raw)
                self._accepted[url] = round(at.timestamp() * 1000) if at else None
            r.published_ms = self._accepted[url]
        while len(self._accepted) > RELEASE_BOOK_MAX:
            del self._accepted[next(iter(self._accepted))]

    def enabled(self) -> bool:
        return "@" in EVENT_LANE["AK_SEC_USER_AGENT"]

    def user_agent(self) -> Optional[str]:
        return EVENT_LANE["AK_SEC_USER_AGENT"]

    async def poll(self, now_ms: int) -> List[AnswerKeyRow]:
        today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
        size = AK_EDGAR_PAGE_SIZE
        out: List[AnswerKeyRow] = []
        for forms in AK_EDGAR_FORMS:
            for page in range(AK_EDGAR_PAGES):
                url = AK_EDGAR_URL.format(
                    forms=quote(forms, safe=","), start=(today - timedelta(days=1)).isoformat(),
                    end=today.isoformat(), offset=page * size)
                raw = await self.client.get(url)
                if raw is None:
                    break
                try:
                    rows = parse(raw)
                    hits = len((json.loads(raw).get("hits") or {}).get("hits") or ())
                except (ValueError, AttributeError) as e:
                    logger.warning(f"Answer key: EDGAR parse failed: {e!r}")
                    break
                out.extend(rows)
                if hits < size:
                    break
        await self._acceptance(out)
        return out
