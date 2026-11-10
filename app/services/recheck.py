"""Later re-check of paid sources."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Optional

import httpx

from alpharidge_ai.schema import Claim
from alpharidge_ai.validator.selector import keyed_fraction
from alpharidge_ai.verify.fetch import FetchedPage, Fetcher, tls_context
from app import config
from app.config import EVENT_LANE
from app.services import ledger, trust, verifier
from app.services.store import ClaimRow, Job, Store
from app.utils.clock import HOUR_MS

# Sampling, timing and notices come from config.PRIVATE.
JOB = "recheck"
GONE = (404, 410)


def _fraction(claim_id: int, purpose: str) -> float:
    return keyed_fraction(config.RECHECK_KEY, claim_id, purpose)


def selected(claim_id: int, route: str, t: float) -> bool:
    if route == "scoop" or t < config.PRIVATE["RECHECK_LOW_TRUST"]:
        return True
    return _fraction(claim_id, "sample") < config.PRIVATE["RECHECK_SAMPLE_RATE"]


def delay_ms(claim_id: int) -> int:
    lo = config.PRIVATE["RECHECK_MIN_H"]
    hi = max(lo, config.PRIVATE["RECHECK_MAX_H"])
    return int((lo + (hi - lo) * _fraction(claim_id, "delay")) * HOUR_MS)


async def schedule(store: Store, row: ClaimRow, route: str, t: float, now_ms: int) -> bool:
    if not selected(row.id, route, t):
        return False
    await store.add_job(JOB, row.id, now_ms + delay_ms(row.id))
    return True


def notice(text: str) -> bool:
    """The page carries a retraction or correction notice."""
    low = " ".join(text.lower().split())
    return any(n.lower() in low for n in config.PRIVATE["RECHECK_NOTICES"])


def check_proxy() -> None:
    """A re-check proxy must keep fetches to public addresses."""
    if (EVENT_LANE["RECHECK_PROXY"] and EVENT_LANE["FETCH_PUBLIC_ONLY"]
            and not EVENT_LANE["RECHECK_PROXY_PUBLIC_ONLY"]):
        raise RuntimeError("EL_RECHECK_PROXY needs EL_RECHECK_PROXY_PUBLIC_ONLY=true")


def make_fetcher() -> Fetcher:
    check_proxy()
    cfg = replace(verifier.fetch_config(), headers=(
        ("Accept", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", config.PRIVATE["RECHECK_ACCEPT_LANGUAGE"])))
    proxy = EVENT_LANE["RECHECK_PROXY"]
    return Fetcher(cfg, transport=httpx.AsyncHTTPTransport(
        proxy=proxy, verify=tls_context()) if proxy else None)


class Recheck:
    def __init__(self, fetcher: Optional[Fetcher] = None,
                 checker: Optional[verifier.Verifier] = None):
        self.fetcher = fetcher or make_fetcher()
        self.checker = checker

    def _verifier(self) -> verifier.Verifier:
        return self.checker or verifier.get_verifier()

    async def _differs(self, row: ClaimRow, ours: str, now_ms: int, store: Store,
                       strict: bool = False) -> Optional[FetchedPage]:
        """The page as fetched now when it no longer matches, else None; `strict` by the
        claim's trust band."""
        page = await self.fetcher.fetch(row.source_url)
        if page.ok and page.content_hash == ours:
            return None
        if not page.ok and (page.status not in GONE or not strict):
            return None
        if page.ok and notice(page.text):
            await trust.log(store, "flag:notice", row.trust_band, str(row.id),
                            "notice on source page", "recheck", row.source_id, now_ms=now_ms)
            return None
        if not strict:
            claim = Claim.model_validate(row.payload)
            got = self._verifier().check(claim, page, verifier._utc(row.received_ms))
            await trust.log(store, "flag:changed", ours, str(row.id),
                            "edited" if got.passed else "no longer backs the claim",
                            "recheck", row.source_id, now_ms=now_ms)
            return None
        return page

    async def run(self, store: Store, claim_id: int, now_ms: int,
                  head: Optional[int] = None) -> bool:
        """True when the claim was clawed back."""
        row = await store.get_claim(claim_id)
        if row is None or row.status not in trust.OPEN_STATUSES + ("settled",):
            return False
        fetches = [f for f in await store.source_fetches(row.id) if f.content_hash]
        source = await store.get_source(row.source_id) if row.source_id is not None else None
        if not fetches or source is None:
            return False
        # A confirmed scoop rests on its confirming page and receipt, not its own page.
        if row.route == "scoop" and (ledger.judge_record(fetches) or {}).get("confirm"):
            return False
        ours = fetches[-1].content_hash
        strict = row.route == "scoop" or row.trust_band not in trust.TRUSTED_BANDS
        for _ in range(2):
            if await self._differs(row, ours, now_ms, store, strict) is None:
                return False
        await trust.penalize(store, trust.CHANGED, source, row, now_ms, head)
        return True


_default: Optional[Recheck] = None


def get_recheck() -> Recheck:
    global _default
    if _default is None:
        _default = Recheck()
    return _default


async def recheck_job(store: Store, job: Job) -> None:
    if job.claim_id is not None:
        await get_recheck().run(store, job.claim_id, time.time_ns() // 1_000_000)
