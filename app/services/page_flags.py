"""Review flags for page types that may carry user-generated content.

Each fetch stores a few page signals (hashed bylines, contributor markers). The nightly
job groups recent fetches by domain and first path segment and writes `flag:*` entries
to the trust log for `trust review`.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from alpharidge_ai.verify.fetch import BLOCKED, FETCH_FAILED, authors
from app.config import PRIVATE
from app.services import trust
from app.services.store import SourceFetchRow, Store
from app.utils.clock import DAY_MS

# Window, thresholds and contributor markers come from config.PRIVATE.
BYLINES, CONTRIBUTOR, UNDATED = "flag:bylines", "flag:contributor", "flag:undated"


def _byline_key(name: str) -> str:
    return hashlib.sha256(" ".join(name.lower().split()).encode()).hexdigest()[:16]


def signals(html: str, url: str) -> dict:
    """Page signals kept with the fetch: byline hashes and whether a contributor marker shows."""
    people = authors(html)
    names = sorted({_byline_key(p["name"]) for p in people if p.get("name", "").strip()})
    text = " ".join([trust.path_of(url), *(v for p in people for v in p.values())]).lower()
    return {"bylines": names,
            "contributor": any(m.lower() in text for m in PRIVATE["PAGE_FLAG_MARKERS"])}


def section(url: str) -> str:
    """"forbes.com/sites/*": the domain and first path segment, or the domain alone."""
    first = trust.path_of(url).strip("/").split("/", 1)[0]
    keep = first and not first.isdigit() and "." not in first
    return f"{trust.domain_of(url)}/{first + '/' if keep else ''}*"


@dataclass(frozen=True)
class Flag:
    field: str
    pattern: str
    reason: str


def flags(fetches: Sequence[SourceFetchRow]) -> List[Flag]:
    """Flags per section from recent fetches of readable pages."""
    groups: Dict[str, List[SourceFetchRow]] = defaultdict(list)
    for f in fetches:
        url = f.final_url or f.canonical_url
        if url and f.content_hash and f.reason not in (FETCH_FAILED, BLOCKED):
            groups[section(url)].append(f)
    out = []
    for pattern, rows in sorted(groups.items()):
        page = [(f.check or {}).get("page") for f in rows]
        seen = [p for p in page if p is not None]
        names = {n for p in seen for n in p.get("bylines", ())}
        if (len(names) >= PRIVATE["PAGE_FLAG_BYLINES_MIN"]
                and len(names) >= PRIVATE["PAGE_FLAG_BYLINES_SHARE"] * len(seen)):
            out.append(Flag(BYLINES, pattern, f"{len(names)} bylines on {len(seen)} pages"))
        marked = sum(bool(p.get("contributor")) for p in seen)
        if marked >= PRIVATE["PAGE_FLAG_CONTRIBUTOR_MIN"]:
            out.append(Flag(CONTRIBUTOR, pattern, f"contributor markers on {marked} pages"))
        undated = sum(f.published_ms is None for f in rows)
        if (undated >= PRIVATE["PAGE_FLAG_UNDATED_MIN"]
                and undated >= PRIVATE["PAGE_FLAG_UNDATED_SHARE"] * len(rows)):
            out.append(Flag(UNDATED, pattern, f"{undated} of {len(rows)} pages undated"))
    return out


async def _source_id(store: Store, pattern: str) -> Optional[int]:
    domain, _, rest = pattern.partition("/")
    source = trust.match(await store.sources(domain), "http://" + domain + "/" + rest.rstrip("*"))
    return source.id if source is not None else None


async def run(store: Store, now_ms: int) -> int:
    """Write new flags to the trust log; a flag already raised in the window is not repeated."""
    since = now_ms - int(PRIVATE["PAGE_FLAG_DAYS"] * DAY_MS)
    raised: set[Tuple[str, str]] = {
        (e.field, e.new or "") for e in await store.trust_log(actor="detector", since_ms=since)}
    n = 0
    for flag in flags(await store.fetches_since(since)):
        if (flag.field, flag.pattern) in raised:
            continue
        await trust.log(store, flag.field, None, flag.pattern, flag.reason, "detector",
                        await _source_id(store, flag.pattern), now_ms=now_ms)
        n += 1
    return n
