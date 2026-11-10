"""Run claims through the whole lane on a MemoryStore: novelty, fetch, check, resolve, settle."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from alpharidge_ai.schema import Claim, canonical_body, category_of
from alpharidge_ai.verify.check import REJECTED, SUPPORTED, CheckResult
from alpharidge_ai.verify.fetch import FetchedPage
from alpharidge_ai.judge.client import ScriptedJudge
from alpharidge_ai.validator.judge import verdict_body
from app import config
from app.services import judge as judges, line, novelty, points, resolve, trust, verifier
from app.services.entities import EntityIndex, PlaceIndex
from app.services.store import ClaimRow, MemoryStore, SourceRow

T0 = int(datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
HOUR_MS = 3600 * 1000
HEAD = 7_000_000

TRUSTED = {"newsone.com": ("owner:a", 0.9), "wiretwo.org": ("owner:b", 0.8),
           "dailythree.net": ("owner:c", 0.7), "lowblog.net": ("owner:d", 0.1)}


@dataclass
class Page:
    text: str
    published_ms: int
    # Served as a page that could not be read.
    unread: bool = False


class Fetcher:
    """Serves saved pages; nothing touches the network."""

    def __init__(self):
        self.pages: Dict[str, Page] = {}

    async def fetch(self, url):
        p = self.pages.get(url)
        if p is None:
            return FetchedPage(url=url, final_url=url, status=404, reason="fetch_failed")
        if p.unread:
            return FetchedPage(url=url, final_url=url, status=403, reason="blocked", unread=True)
        return FetchedPage(
            url=url, final_url=url, status=200, text=p.text, canonical_url=url,
            published_at=datetime.fromtimestamp(p.published_ms / 1000, tz=timezone.utc),
            content_hash=hashlib.sha256(p.text.encode()).hexdigest(), bytes=len(p.text))

    async def aclose(self):
        pass


class Panel:
    """The listed validators (or one stand-in when none are listed), each judging a new item
    at once with the scripted judge on the API's own fetch of the page. `fixed` is the fixed
    check result they post; `judge` may be one judge for all or one per validator."""

    def __init__(self, judge=None, fixed: bool = True, validators=None):
        self.judge = judge or ScriptedJudge()
        self.fixed, self.validators = fixed, validators
        self.items = []

    def judge_of(self, validator: str):
        return self.judge.get(validator) if isinstance(self.judge, dict) else self.judge

    async def __call__(self, store, item, claim, page):
        self.items.append(item)
        for v in self.validators or sorted(config.REVERIFY_VALIDATORS) or ["panel"]:
            j = self.judge_of(v)
            answers = j.judge_page(claim, page.text, None) if self.fixed else {}
            body = verdict_body({"key": item.key, **item.task}, claim, page.content_hash,
                                answers, self.fixed, None if self.fixed else "unsupported")
            await judges.add_post(store, v, body, 0, "n", "00", item.created_ms)


class Fixed(verifier.Verifier):
    """A verifier whose check gives one fixed answer."""

    def __init__(self, passed: bool, **kw):
        super().__init__(**kw)
        self.passed = passed

    def check(self, claim, page, received, scoop=False):
        if page.unread or not page.ok:
            return super().check(claim, page, received, scoop)
        if not self.passed:
            return CheckResult(REJECTED, "unsupported")
        return CheckResult(SUPPORTED, spans=((0, 1),))


def payload(**kw) -> dict:
    base = dict(kind="event", headline="Acme to acquire Beta Systems",
                event_type="merger_acquisition", category="finance", event_date="2026-10-13",
                actors=[{"id_scheme": "wikidata", "id": "Q1", "role": "acquirer",
                         "name": "Acme Inc."},
                        {"id_scheme": "wikidata", "id": "Q2", "role": "target",
                         "name": "Beta Systems"}],
                facts=[], source_url="https://newsone.com/a",
                quote="Acme Inc. will acquire Beta Systems.")
    base.update(kw)
    return json.loads(canonical_body(Claim(**base)))


class Lane:
    def __init__(self, check: Optional[bool] = True, entities: Optional[EntityIndex] = None,
                 judge=None):
        self.store = MemoryStore()
        self.fetcher = Fetcher()
        self.entities = entities if entities is not None else EntityIndex()
        self.panel = Panel(judge or ScriptedJudge({"subject": "yes"}))
        kw = dict(fetcher=self.fetcher, panel=self.panel, entities=self.entities)
        self.verifier = verifier.Verifier(**kw) if check is None else Fixed(check, **kw)
        self.resolver = resolve.Resolver(entities=self.entities, registry=self.verifier.registry)
        self.places = PlaceIndex()
        self.head = HEAD
        self._n = 0

    async def sources(self, table=TRUSTED):
        for domain, (owner, t) in table.items():
            await self.store.add_source(SourceRow(domain=domain, owner_id=owner,
                                                  status="active", base_trust=t))

    def page(self, url: str, text: Optional[str] = None, published_ms: int = T0):
        if text is None:
            words = (hashlib.sha256(f"{url}{i}".encode()).hexdigest()[:8] for i in range(40))
            text = " ".join(words) + ". Acme Inc. will acquire Beta Systems."
        self.fetcher.pages[url] = Page(text, published_ms)

    async def submit(self, hotkey: str, received_ms: int, coldkey=None, **kw) -> ClaimRow:
        p = payload(**kw)
        self._n += 1
        if p["source_url"] not in self.fetcher.pages:
            self.page(p["source_url"], published_ms=received_ms - 60_000)
        category = category_of(p["event_type"]).value
        return await self.store.create_claim(ClaimRow(
            hotkey=hotkey, signature="00", nonce=f"n{self._n}", signed_at=0,
            body_hash=f"h{self._n}", body=canonical_body(Claim(**p)).decode(),
            received_ms=received_ms, bucket=received_ms // 250, kind=p["kind"],
            category=category, event_type=p["event_type"], payload=p,
            source_url=p["source_url"], status="queued", coldkey=coldkey))

    async def step(self, now_ms: int) -> None:
        for row in await line.ready_claims(self.store, now_ms):
            source = await trust.resolve(self.store, row.source_url, now_ms)
            ct = await trust.of_claim(self.store, self.verifier.registry, self.entities,
                                      Claim.model_validate(row.payload), source, row.category,
                                      row.source_url)
            await novelty.apply(self.store, row, self.entities, self.places, now_ms, trust=ct.t)
        for row in await self.store.claims_by_status("novelty"):
            await self.verifier.fetch(self.store, row, now_ms=now_ms)
            await self.verifier.verify(self.store, row, now_ms, near=[])
        await self.resolver.run(self.store, now_ms, head=self.head)

    async def settle(self, now_ms: int) -> None:
        await self.resolver.close_windows(self.store, now_ms)
        await points.settle_due(self.store, now_ms, head=self.head)

    def event_of(self, row: ClaimRow) -> int:
        return self.store.claims[row.id].event_id

    def paid(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for r in self.store.ledger:
            out[r.hotkey] = out.get(r.hotkey, 0.0) + r.points
        return out

    def by_route(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for r in self.store.ledger:
            out[r.route] = out.get(r.route, 0.0) + r.points
        return out
