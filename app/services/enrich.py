"""Event enrichment: a summary, sentiment and place for each published event, read from its
page by one model call, plus a search embedding.

Off unless EL_ENRICH_OPENROUTER_KEY is set (app.config.ENRICH has the rest). Every request pins
its model, refuses fallbacks and carries a max price; each call's reported cost counts toward a
daily cap past which the worker pauses. Enrichment never touches claims, pay or the feed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Optional, Tuple

import httpx

from app import config
from app.services import quality
from app.services.store import WON, EnrichRow, EventRecord, Store

logger = logging.getLogger(__name__)

CONFIRMED, REPORTED, UNVERIFIED = "confirmed", "reported", "unverified"
SENTIMENTS = ("positive", "negative", "neutral", "mixed")
DONE, FAILED, SKIPPED = "done", "failed", "skipped"
EMBED_DIM = 384
LEASE_MS = 180_000
DISCOVER_S = 60
DISCOVER_LIMIT = 500
SUMMARY_MAX = 800
RETRY_MAX_S = 6 * 3600

SYSTEM = (
    "You enrich one news event from the page text. Reply with JSON only: "
    '{"summary": str, "sentiment": {"label": "positive"|"negative"|"neutral"|"mixed", '
    '"score": float}, "country": str|null, "region": str|null}. '
    "summary: 1-2 factual sentences on what happened, who, and when, from the page only. "
    "sentiment: tone of the event for its main subject, score -1 to 1. "
    "country: ISO 3166-1 alpha-2 of the country where the event happens: where the company, "
    "body or people it is about act, not where the publisher is. Most events have one. null "
    "only when the event genuinely involves two or more countries as parties (one country "
    "sanctioning a company in another, a company buying one in another country) or is global. "
    "region: first-level subdivision (state, province) in English when country is set and the "
    "page names one, else null.")


class CallFailed(Exception):
    """A call that returned nothing usable; `refused` when the router turned it away (max
    price, no endpoint, no credit)."""

    def __init__(self, what: str, refused: bool = False):
        super().__init__(what)
        self.refused = refused


def enabled() -> bool:
    return bool(config.ENRICH_KEY)


def check_settings() -> None:
    """Models must be pinned slugs, never a router alias."""
    for k in ("MODEL", "EMBED_MODEL"):
        slug = config.ENRICH[k]
        if not slug or slug.startswith("~") or slug.endswith("latest") or "/" not in slug:
            raise RuntimeError(f"EL_ENRICH_{k} must be a pinned model slug, not {slug!r}")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def utc_day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _provider(**prices) -> dict:
    return {"allow_fallbacks": False, "max_price": prices}


def _cost(usage: dict, prompt_max: float, completion_max: float = 0.0) -> float:
    """The call's reported cost, else its tokens at the max prices."""
    c = usage.get("cost")
    if isinstance(c, (int, float)) and not isinstance(c, bool) and c >= 0:
        return float(c)
    p = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    o = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return (p * prompt_max + o * completion_max) / 1e6


class Router:
    """OpenRouter calls: chat and embeddings. Each returns (result, cost)."""

    def __init__(self, key: str, url: str, timeout_s: float,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = url
        self.client = httpx.AsyncClient(
            timeout=timeout_s, transport=transport,
            headers={"authorization": f"Bearer {key}", "content-type": "application/json"})

    async def _post(self, path: str, body: dict) -> dict:
        try:
            r = await self.client.post(self.url + path, json=body)
        except httpx.HTTPError as e:
            raise CallFailed(f"{body['model']}: {type(e).__name__}") from None
        if r.status_code >= 400:
            refused = r.status_code in (402, 404) or "max price" in r.text.lower()
            raise CallFailed(f"{body['model']}: HTTP {r.status_code}", refused=refused)
        try:
            return r.json()
        except ValueError:
            raise CallFailed(f"{body['model']}: not JSON") from None

    async def chat(self, body: dict, prompt_max: float, completion_max: float
                   ) -> Tuple[dict, float]:
        got = await self._post("/v1/chat/completions", body)
        cost = _cost(got.get("usage") or {}, prompt_max, completion_max)
        try:
            text = got["choices"][0]["message"]["content"].strip().strip("`")
            out = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise CallFailed(f"{body['model']}: no JSON answer") from None
        if not isinstance(out, dict):
            raise CallFailed(f"{body['model']}: no JSON answer")
        return out, cost

    async def embed(self, body: dict, prompt_max: float) -> Tuple[list, float]:
        got = await self._post("/v1/embeddings", body)
        try:
            vec = [float(v) for v in got["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError):
            raise CallFailed(f"{body['model']}: no embedding") from None
        if len(vec) != EMBED_DIM:
            raise CallFailed(f"{body['model']}: {len(vec)} dimensions")
        return vec, _cost(got.get("usage") or {}, prompt_max)


# Requests

def page_state(ev: EventRecord, url: str, text: str) -> list:
    return [f"Event: {ev.headline}", f"Type: {ev.event_type}",
            f"Date: {ev.event_date.isoformat()}", f"URL: {url}", "Page text:",
            text[:config.ENRICH["PAGE_CHARS"]]]


def chat_body(ev: EventRecord, url: str, text: str) -> dict:
    s = config.ENRICH
    return {"model": s["MODEL"], "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "\n".join(page_state(ev, url, text))}],
            "reasoning": {"effort": "minimal"}, "response_format": {"type": "json_object"},
            "usage": {"include": True},
            "provider": _provider(prompt=s["MAX_PROMPT"], completion=s["MAX_COMPLETION"])}


def embed_body(ev: EventRecord, summary: Optional[str]) -> dict:
    s = config.ENRICH
    text = ev.headline + (f". {summary}" if summary else "")
    return {"model": s["EMBED_MODEL"], "input": text,
            "provider": _provider(prompt=s["EMBED_MAX_PROMPT"])}


# The answers

def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse(out: dict) -> dict:
    """The model's JSON as clean fields; anything malformed is None."""
    summary = out.get("summary")
    summary = " ".join(summary.split())[:SUMMARY_MAX] if isinstance(summary, str) else None
    sent = out.get("sentiment") if isinstance(out.get("sentiment"), dict) else {}
    label = sent.get("label") if sent.get("label") in SENTIMENTS else None
    score = _num(sent.get("score"))
    score = max(-1.0, min(1.0, score)) if score is not None and label else None
    country = out.get("country")
    country = country.strip().upper() if isinstance(country, str) else None
    country = country if country and re.fullmatch(r"[A-Z]{2}", country) else None
    region = out.get("region")
    region = " ".join(region.split())[:100] if isinstance(region, str) and country else None
    return {"summary": summary or None, "sentiment": label, "score": score,
            "country": country, "region": region or None}


# Which events

async def qualifies(store: Store, ev: EventRecord, owner: Optional[str]) -> bool:
    """Confirmed and reported events; a scoop (unverified with no first claim) when its
    coldkey's pay multiplier is at least SCOOP_QUALITY_MIN over at least SCOOP_MIN_JUDGED
    decided claims. Never a line or a labelled event."""
    if ev.hold is not None or ev.publish_ms is None:
        return False
    if ev.confidence in (CONFIRMED, REPORTED):
        return True
    if ev.confidence != UNVERIFIED or ev.first_claim_id is not None or not owner:
        return False
    mult, judged = await quality.pay_standing_of(store, owner)
    return (judged >= config.ENRICH["SCOOP_MIN_JUDGED"]
            and mult >= config.ENRICH["SCOOP_QUALITY_MIN"])


async def scoop_owner(store: Store, ev: EventRecord) -> Optional[str]:
    scoops = [c for c in await store.claims_for_event(ev.id, WON) if c.route == "scoop"]
    first = min(scoops, key=lambda c: c.id, default=None)
    return (first.coldkey or first.hotkey) if first else None


async def discover(store: Store, now_ms: int, since_ms: Optional[int] = None,
                   limit: int = DISCOVER_LIMIT, backfill: bool = False,
                   dry_run: bool = False) -> Tuple[int, int]:
    """Queue the published events that have no row yet: pending when they qualify, else
    skipped (reopened once they are confirmed or reported). Returns (queued, skipped)."""
    if since_ms is None:
        since_ms = now_ms - int(config.ENRICH["LOOKBACK_H"] * 3_600_000)
    found = await store.enrich_candidates((CONFIRMED, REPORTED), since_ms, now_ms, limit)
    yes, no = [], []
    for ev, owner in found:
        (yes if await qualifies(store, ev, owner) else no).append(ev.id)
    if dry_run:
        return len(yes), len(no)
    if not backfill:
        await store.reopen_enrichments((CONFIRMED, REPORTED), now_ms)
    return (await store.add_enrichments(yes, now_ms, backfill=backfill),
            await store.add_enrichments(no, now_ms, backfill=backfill, status=SKIPPED))


# The work

async def _page(ev: EventRecord, store: Store) -> Tuple[str, str]:
    """The URL and text of the page the event rests on: its first claim's, else its scoop's."""
    from app.services import verifier

    row = await store.get_claim(ev.first_claim_id) if ev.first_claim_id is not None else None
    if row is None:
        scoops = [c for c in await store.claims_for_event(ev.id, WON) if c.route == "scoop"]
        row = min(scoops, key=lambda c: c.id, default=None)
    if row is None:
        raise CallFailed("no source claim")
    v = verifier.get_verifier()
    page = v.cache.get(row.source_url, row.received_ms) or await v.fetcher.fetch(row.source_url)
    if not page.ok or not page.text.strip():
        raise CallFailed(f"page: {page.reason or 'no text'}")
    return page.final_url or row.source_url, page.text


class Spend:
    """The day's spend, read from the store and added to after each call."""

    def __init__(self, store: Store):
        self.store = store
        self._alarmed: Optional[date] = None

    async def today(self, now_ms: int) -> float:
        return await self.store.enrich_spend(utc_day(now_ms))

    async def add(self, now_ms: int, usd: float) -> None:
        if usd > 0:
            await self.store.add_enrich_spend(utc_day(now_ms), usd)

    async def over(self, now_ms: int) -> bool:
        spent, cap = await self.today(now_ms), config.ENRICH["DAILY_USD"]
        if spent < cap:
            return False
        day = utc_day(now_ms)
        if self._alarmed != day:
            self._alarmed = day
            logger.error(f"[ALARM] enrich_spend: ${spent:.4f} spent on {day}, cap "
                         f"${cap:.2f}; enrichment paused until the next UTC day")
        return True


def _logged(ev: EventRecord, model: str, cost: float) -> float:
    logger.info(f"[ENRICH] event {ev.id} {model} cost ${cost:.7f}")
    return cost


def _retry_ms(now_ms: int, attempts: int) -> int:
    wait = min(RETRY_MAX_S, config.ENRICH["RETRY_S"] * 2 ** max(0, attempts - 1))
    return now_ms + int(wait * 1000)


async def enrich_one(store: Store, router: Router, spend: Spend, row: EnrichRow,
                     now_ms: Optional[int] = None) -> str:
    """Enrich one taken row; returns its outcome (done, skipped, failed or retry)."""
    from alpharidge_ai.verify.fetch import Paced

    now_ms = now_ms or _now_ms()
    s = config.ENRICH
    ev = await store.get_event(row.event_id)
    owner = await scoop_owner(store, ev) if ev is not None and ev.first_claim_id is None else None
    if ev is None or not await qualifies(store, ev, owner):
        await store.finish_enrichment(replace(row, status=SKIPPED, done_ms=now_ms))
        return SKIPPED
    last = row.attempts >= s["MAX_ATTEMPTS"]
    cost, out, vec, error = 0.0, None, None, None
    try:
        url, text = await _page(ev, store)
        got, c = await router.chat(chat_body(ev, url, text), s["MAX_PROMPT"],
                                   s["MAX_COMPLETION"])
        cost += _logged(ev, s["MODEL"], c)
        out = parse(got)
        if not out["summary"]:
            raise CallFailed(f"{s['MODEL']}: no summary")
        vec, c = await router.embed(embed_body(ev, out["summary"]), s["EMBED_MAX_PROMPT"])
        cost += _logged(ev, s["EMBED_MODEL"], c)
    except Paced as e:
        await spend.add(now_ms, cost)
        await store.defer_enrichment(row.event_id, row.attempts,
                                     now_ms + int(e.wait_s * 1000) + 1, "paced", count=False)
        return "retry"
    except Exception as e:
        error = repr(e)[:300] if not isinstance(e, CallFailed) else str(e)[:300]
    await spend.add(now_ms, cost)
    if error is not None and not (last and out is not None and out["summary"]):
        if not last:
            logger.warning(f"[ENRICH] event {ev.id} attempt {row.attempts}: {error}")
            await store.defer_enrichment(row.event_id, row.attempts,
                                         _retry_ms(now_ms, row.attempts), error)
            return "retry"
        logger.warning(f"[ENRICH] event {ev.id} failed after {row.attempts} attempts: {error}")
        await store.finish_enrichment(replace(row, status=FAILED, error=error,
                                              cost_usd=row.cost_usd + cost, done_ms=now_ms))
        return FAILED
    done = replace(row, status=DONE, confidence=ev.confidence, summary=out["summary"],
                   sentiment=out["sentiment"], sentiment_score=out["score"],
                   country=out["country"], region=out["region"],
                   cost_usd=row.cost_usd + cost, error=error, done_ms=now_ms)
    if not await store.finish_enrichment(done, vec):
        logger.warning(f"[ENRICH] event {ev.id}: row moved on, result dropped")
    return DONE


async def run_once(store: Store, router: Router, spend: Spend, now_ms: int) -> int:
    """One pass: take due rows while the day's spend is under the cap, and enrich them."""
    if await spend.over(now_ms):
        return 0
    backfill = await spend.today(now_ms) < config.ENRICH["BACKFILL_DAILY_USD"]
    rows = await store.take_enrichments(config.ENRICH["CONCURRENCY"], LEASE_MS, now_ms,
                                        backfill)
    got = await asyncio.gather(*(enrich_one(store, router, spend, r, now_ms) for r in rows),
                               return_exceptions=True)
    for r, g in zip(rows, got):
        if isinstance(g, BaseException):
            logger.warning(f"[ENRICH] event {r.event_id}: {g!r}")
    return len(rows)


def make_router() -> Router:
    return Router(config.ENRICH_KEY, config.ENRICH["URL"], config.ENRICH["TIMEOUT_S"])


async def run_forever(store: Store) -> None:
    check_settings()
    router, spend, last_discover = make_router(), Spend(store), 0.0
    logger.info(f"[ENRICH] on: {config.ENRICH['MODEL']}, cap "
                f"${config.ENRICH['DAILY_USD']:.2f}/day")
    while True:
        try:
            now_ms = _now_ms()
            if time.monotonic() - last_discover >= DISCOVER_S:
                last_discover = time.monotonic()
                await discover(store, now_ms)
            await run_once(store, router, spend, now_ms)
        except Exception as e:
            logger.error(f"[ENRICH] pass failed: {e!r}")
        await asyncio.sleep(config.ENRICH["INTERVAL_S"])
