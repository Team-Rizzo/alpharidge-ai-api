"""Verifier: fetch on arrival, the shared check after novelty, outcomes on the claim."""

import json
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from alpharidge_ai.schema import Claim, canonical_body
from alpharidge_ai.verify.fetch import BLOCKED, FetchConfig, FetchedPage, Fetcher
from app.config import EVENT_LANE
from alpharidge_ai.judge.client import ScriptedJudge
from app.services import trust, verifier
from app.services.store import ClaimRow, EventRecord, Job, MemoryStore, SourceRow

RECEIVED_MS = int(datetime(2026, 10, 13, 12, 40, tzinfo=timezone.utc).timestamp() * 1000)
DEAL = "Acme Inc. agreed on Tuesday to acquire Beta Systems in a deal valued at $1.2 billion."
REJECTED = "Beta Systems rejected a $1.2 billion offer from Acme Inc. to acquire it."
FILLER = ("The companies said the boards of both firms have approved the terms. The deal is "
          "expected to close next year, subject to the usual conditions for a deal this size.")


def html(body, published="2026-10-13T12:00:00Z", free=True):
    ld = {"@type": "NewsArticle", "isAccessibleForFree": free}
    if published:
        ld["datePublished"] = published
    return (f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
            f"<body><article><h1>Acme and Beta</h1><p>{body}</p><p>{FILLER}</p></article>"
            "</body></html>").encode()


PAGES = {
    "/robots.txt": b"User-agent: *\nAllow: /\n",
    "/supported": html(DEAL),
    "/contradicted": html("Acme Inc. agreed on Tuesday to acquire Beta Systems for $900 million."),
    "/rejected": html(REJECTED),
    "/old": html(DEAL, published="2026-09-09T08:00:00Z"),
    "/paywall": html("Subscribe to keep reading.", free=False),
}


class _Handler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        self.hits.append(self.path)
        body = PAGES.get(self.path.split("?")[0])
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body or b"not found")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make(judge=None, **kw):
    from tests.pipeline import Panel
    cfg = FetchConfig(public_only=False, domain_rps=1000, retry_delay_s=0)
    return verifier.Verifier(fetcher=Fetcher(cfg), panel=Panel(judge or ScriptedJudge(), **kw))


def payload(url, **kw):
    base = dict(
        kind="event", headline="Acme to acquire Beta Systems", event_type="merger_acquisition",
        category="finance", event_date="2026-10-13",
        actors=[{"id_scheme": "wikidata", "id": "Q1", "role": "acquirer", "name": "Acme Inc."},
                {"id_scheme": "other", "id": "beta", "role": "target", "name": "Beta Systems"}],
        facts=[{"field": "deal_value", "value": 1.2, "unit": "usd_billions"}], source_url=url,
        quote=DEAL)
    base.update(kw)
    return json.loads(canonical_body(Claim(**base)))


async def submit(store, url, route="event", trusted=True, **kw):
    """A claim on `url`; the local test site is a trusted source unless `trusted` is off."""
    domain = trust.domain_of(url)
    if trusted and not await store.sources(domain):
        await store.add_source(SourceRow(domain=domain, owner_id="test-site", status="active",
                                         base_trust=1.0))
    p = payload(url, **kw)
    n = len(store.claims)
    return await store.create_claim(ClaimRow(
        hotkey="5Fminer", signature="00", nonce=f"{url}{n}", signed_at=0, body_hash=f"{url}{n}",
        body="", received_ms=RECEIVED_MS, bucket=RECEIVED_MS // 250, kind=p["kind"],
        category="finance", event_type=p["event_type"], payload=p, source_url=url,
        status="novelty", novelty="new", route=route))


async def run(v, store, row, near=()):
    for _ in range(verifier.FETCH_TRIES):
        page, _ = await v.fetch(store, row)
        if page.ok or page.reason == BLOCKED:
            break
    return await v.verify(store, row, RECEIVED_MS + 1000, near=list(near))


async def test_supported_page_is_fetched_with_hashes_not_text(base):
    store, v = MemoryStore(), make()
    row = await submit(store, f"{base}/supported")
    await store.update_claim(row.id, "novelty", event_id=7)
    row = await run(v, store, await store.get_claim(row.id))
    assert (row.status, row.reason) == ("fetched", None)
    assert row.doc_hash and row.category == "finance"
    (seen,) = store.source_event_rows
    assert row.source_id == seen.source_id and (seen.role, seen.event_id) == ("first", 7)
    (fetch,) = store.fetches
    assert fetch.verdict == "supported" and fetch.spans and fetch.http_status == 200
    assert fetch.content_hash and fetch.published_ms and not hasattr(fetch, "text")
    await v.fetcher.aclose()


async def test_with_scoops_off_an_untrusted_page_is_turned_away_before_the_judge(base,
                                                                                 monkeypatch):
    monkeypatch.setitem(EVENT_LANE, "SCOOPS", False)
    store, v = MemoryStore(), make()
    row = await submit(store, f"{base}/supported", route="scoop", trusted=False)
    row = await run(v, store, row)
    assert (row.status, row.reason) == ("rejected", "scoop_limit")
    assert v.panel.judge.calls == 0 and not v.panel.items
    await v.fetcher.aclose()


@pytest.mark.parametrize("path,reason", [
    ("/contradicted", "unsupported"), ("/rejected", "judge_reject")])
async def test_wrong_figures_and_judge_noes_are_rejected_with_what_failed(base, path, reason):
    store = MemoryStore()
    v = make(ScriptedJudge(script={"/rejected": {"event": "no"}}))
    row = await run(v, store, await submit(store, base + path, quote=REJECTED))
    assert (row.status, row.reason) == ("rejected", reason)
    assert row.judged_ms
    # Each names what failed: the fixed check its missing parts, the judge its questions.
    detail = ({"missing": ["fact:deal_value"]} if reason == "unsupported"
              else {"judge": ["event"]})
    assert store.fetches[-1].check["detail"] == detail
    await v.fetcher.aclose()


async def test_a_third_failed_judging_of_a_page_is_turned_away(base):
    store = MemoryStore()
    v = make(ScriptedJudge(script={"/rejected": {"event": "no"}}))
    got = [(await run(v, store, await submit(store, base + "/rejected", quote=REJECTED,
                                              headline=f"Acme {i}"))).reason for i in range(3)]
    assert got == ["judge_reject", "judge_reject", "judge_repeat"]
    await v.fetcher.aclose()


@pytest.mark.parametrize("path,reason", [
    ("/old", "stale_date"), ("/missing", "fetch_failed"), ("/paywall", "blocked")])
async def test_stale_missing_and_blocked_pages_are_rejected(base, path, reason):
    store, v = MemoryStore(), make()
    row = await run(v, store, await submit(store, base + path, quote=REJECTED))
    assert (row.status, row.reason) == ("rejected", reason)
    await v.fetcher.aclose()


async def test_a_fetch_job_ending_after_the_claim_closed_adds_no_row(base, monkeypatch):
    store, v = MemoryStore(), make()
    row = await submit(store, f"{base}/old", quote=REJECTED)
    real = v.fetcher.fetch

    async def raced(url):
        page = await real(url)
        await store.update_claim(row.id, "novelty", status="rejected", reason="stale_date")
        return page

    monkeypatch.setattr(v.fetcher, "fetch", raced)
    monkeypatch.setattr(verifier, "_default", v)
    await verifier.fetch_job(store, Job(id=1, kind="fetch", claim_id=row.id))
    assert store.fetches == []
    await v.fetcher.aclose()


async def test_page_rules_exclude(base):
    class Contributor:
        def page_excluded(self, url, page):
            return "contributor"

    store, v = MemoryStore(), make()
    v.rules = Contributor()
    row = await run(v, store, await submit(store, f"{base}/supported"))
    assert row.reason == "page_type"
    await v.fetcher.aclose()


async def test_contrastive_known_event_is_rejected(base):
    store, v = MemoryStore(), make()
    ev = await store.add_event(EventRecord(
        event_type="merger_acquisition", category="finance", event_date=date(2026, 10, 12),
        fingerprint="fp", names=("Beta Systems",), subject_keys=("name:beta systems",)))
    target = [{"id_scheme": "other", "id": "beta", "role": "target", "name": "Beta"}]
    row = await run(v, store, await submit(store, f"{base}/supported", actors=target), near=[ev])
    assert (row.status, row.reason) == ("rejected", "contrastive_known")
    assert (row.novelty, row.event_id) == ("known", ev.id)
    await v.fetcher.aclose()


async def test_contrastive_keeps_a_different_subject_new(base):
    store, v = MemoryStore(), make()
    ev = await store.add_event(EventRecord(
        event_type="merger_acquisition", category="finance", event_date=date(2026, 10, 12),
        fingerprint="fp", names=("Acme Inc.",), subject_keys=("name:acme",)))
    target = [{"id_scheme": "other", "id": "beta", "role": "target", "name": "Beta Systems"}]
    row = await run(v, store, await submit(store, f"{base}/supported", actors=target), near=[ev])
    assert row.event_id != ev.id and row.reason != "contrastive_known"
    await v.fetcher.aclose()


def test_page_cache_keeps_good_pages_for_earlier_claims():
    cache = verifier.PageCache(ttl_s=60)
    cache.put("u", FetchedPage(url="u", html="<p>x</p>", text="x"), fetched_ms=100)
    assert cache.get("u", 50).html == "" and cache.get("u", 100).text == "x"
    assert cache.get("u", 150) is None
    cache.put("f", FetchedPage(url="f", reason="fetch_failed"), fetched_ms=100)
    assert cache.get("f", 50) is None


def test_page_cache_drops_expired_pages_and_keeps_to_its_size(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(verifier.time, "monotonic", lambda: clock[0])
    cache = verifier.PageCache(ttl_s=60, max_bytes=3200)
    cache.put("a", FetchedPage(url="a", text="x" * 1000), fetched_ms=1)
    clock[0] = 30.0
    cache.put("b", FetchedPage(url="b", text="x" * 1000), fetched_ms=1)
    clock[0] = 61.0
    cache.put("c", FetchedPage(url="c", text="x" * 1000), fetched_ms=1)
    assert list(cache._pages) == ["b", "c"]
    cache.put("d", FetchedPage(url="d", text="x" * 1000), fetched_ms=1)
    assert list(cache._pages) == ["c", "d"] and cache._bytes <= 3200
    cache.put("e", FetchedPage(url="e", text="x" * 5000), fetched_ms=1)
    assert cache.get("e", 0) is None and cache.get("d", 0) is not None
    cache.put("w", FetchedPage(url="w", text="\u20ac" * 1000), fetched_ms=1)
    assert cache.get("w", 0) is None and list(cache._pages) == ["c", "d"]


async def test_failed_fetch_is_retried_and_not_reused(base):
    store, v = MemoryStore(), make()
    url = f"{base}/late"
    early = await submit(store, url)
    assert not (await v.fetch(store, early))[0].ok
    retry_ms = store.fetches[-1].fetched_ms + verifier.FETCH_REQUEST_S * 1000
    assert (await v.verify(store, early, retry_ms)).status == "novelty"
    assert [j.claim_id for j in await store.take_jobs(["fetch"], 10, 60_000, retry_ms)] == \
        [early.id]
    PAGES["/late"] = html(DEAL)
    try:
        later = await run(v, store, await submit(store, url))
        assert (later.status, later.reason) == ("fetched", None)
        early = await v.verify(store, early, retry_ms + 1000)
        assert (early.status, early.reason) == ("fetched", None)
    finally:
        del PAGES["/late"]
    await v.fetcher.aclose()
