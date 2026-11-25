"""Event enrichment: which events qualify, the stored answer, the cost guards and claiming."""

import asyncio
import json
import uuid
from datetime import date

import httpx
import pytest

from app import config
from app.services import enrich, ops, product, quality
from app.services.store import ClaimRow, EnrichRow, EventRecord, MemoryStore

T0 = 1_795_000_000_000
DAY = enrich.utc_day(T0)


def _luna(country="US", region="California", label="positive", score=0.5,
          summary="Acme bought Beta on 1 Nov."):
    return {"summary": summary, "sentiment": {"label": label, "score": score},
            "country": country, "region": region}


class Fake:
    """OpenRouter on a mock transport: records each request and answers per path."""

    def __init__(self):
        self.sent = []
        self.luna = _luna()
        self.fail = {}
        self.cost = {"completions": 0.0002, "embeddings": 0.0000001}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        self.sent.append((path, body))
        if path in self.fail:
            return httpx.Response(self.fail[path][0], text=self.fail[path][1])
        usage = {"cost": self.cost[path]}
        if path == "completions":
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(self.luna)}}], "usage": usage})
        return httpx.Response(200, json={"data": [{"embedding": [0.01] * 384}],
                                         "usage": usage})

    def router(self) -> enrich.Router:
        return enrich.Router("k", "https://router.test/api", 5,
                             transport=httpx.MockTransport(self.handler))

    def calls(self, path):
        return [b for p, b in self.sent if p == path]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "ENRICH_KEY", "test-enrich-key")

    async def page(ev, store):
        return "https://news.example/a", "Acme Inc. bought Beta Systems in California."
    monkeypatch.setattr(enrich, "_page", page)
    return Fake()


async def _event(store, confidence="reported", first_claim=True, publish_ms=T0 - 60_000,
                 hold=None, coldkey="ck1") -> EventRecord:
    ev = await store.add_event(EventRecord(
        event_type="merger_acquisition", category="finance", event_date=date(2026, 11, 1),
        fingerprint=uuid.uuid4().hex, headline="Acme Inc. and Beta Systems: acquisition",
        confidence=confidence, publish_ms=publish_ms, hold=hold))
    row = await store.create_claim(ClaimRow(
        hotkey="hk1", signature="s", nonce=uuid.uuid4().hex, signed_at=T0 // 1000,
        body_hash=uuid.uuid4().hex, body="{}", received_ms=T0 - 120_000, bucket=0,
        kind="event", category="finance", event_type="merger_acquisition", payload={},
        source_url="https://news.example/a", event_id=ev.id, coldkey=coldkey,
        route="event" if first_claim else "scoop", status="verified"))
    if first_claim:
        await store.update_event(ev.id, first_claim_id=row.id)
    return await store.get_event(ev.id)


async def _run(store, fake, now=T0):
    return await enrich.run_once(store, fake.router(), enrich.Spend(store), now)


def test_off_by_default():
    assert config.ENRICH_KEY == "" and not enrich.enabled()


async def test_off_raises_no_alarm_and_calls_nothing():
    store = MemoryStore()
    await store.add_enrich_spend(DAY, 99.0)
    assert await ops._enrich_spend(store, T0) == []


async def test_which_events_qualify(on, monkeypatch):
    store = MemoryStore()
    standing = {"good": (0.85, 40), "poor": (0.4, 40), "few": (1.0, 19)}

    async def of(store, owner):
        return standing[owner]
    monkeypatch.setattr(quality, "pay_standing_of", of)
    confirmed = await _event(store, "confirmed")
    reported = await _event(store, "reported")
    good = await _event(store, "unverified", first_claim=False, coldkey="good")
    poor = await _event(store, "unverified", first_claim=False, coldkey="poor")
    few = await _event(store, "unverified", first_claim=False, coldkey="few")
    labelled = await _event(store, "unverified")
    line = await _event(store, "line")
    held = await _event(store, "reported", hold="person")
    old = await _event(store, "reported", publish_ms=T0 - 30 * 3_600_000)
    unpublished = await _event(store, "reported", publish_ms=T0 + 60_000)

    assert await enrich.discover(store, T0) == (3, 2)
    rows = store.enrich_rows
    assert {i for i, r in rows.items() if r.status == "pending"} == {
        confirmed.id, reported.id, good.id}
    assert rows[poor.id].status == rows[few.id].status == "skipped"
    for ev in (labelled, line, held, old, unpublished):
        assert ev.id not in rows
    assert await enrich.discover(store, T0) == (0, 0)

    await store.update_event(poor.id, confidence="confirmed")
    await enrich.discover(store, T0 + 1)
    assert store.enrich_rows[poor.id].status == "pending"


async def _decided(store, coldkey, n, junk=False):
    status, reason = ("rejected", "judge_reject") if junk else ("settled", None)
    for _ in range(n):
        await store.create_claim(ClaimRow(
            hotkey="hk1", signature="s", nonce=uuid.uuid4().hex, signed_at=T0 // 1000,
            body_hash=uuid.uuid4().hex, body="{}", received_ms=T0 - 120_000, bucket=0,
            kind="event", category="finance", event_type="merger_acquisition", payload={},
            source_url="https://news.example/a", coldkey=coldkey, route="event",
            status=status, reason=reason, judged_ms=T0 - 110_000, decided_ms=T0 - 100_000))


async def test_new_coldkey_scoop_does_not_qualify_until_judged(on, monkeypatch):
    monkeypatch.setattr(quality, "_now_ms", lambda: T0)
    monkeypatch.setitem(quality.EVENT_LANE, "BUDGET_FROM_MS", 0)
    store = MemoryStore()
    ev = await _event(store, "unverified", first_claim=False, coldkey="fresh")
    quality.reset()
    assert await quality.pay_standing_of(store, "fresh") == (pytest.approx(0.8), 0)
    assert not await enrich.qualifies(store, ev, "fresh")
    await _decided(store, "fresh", config.ENRICH["SCOOP_MIN_JUDGED"])
    quality.reset()
    mult, judged = await quality.pay_standing_of(store, "fresh")
    assert judged == config.ENRICH["SCOOP_MIN_JUDGED"] and mult >= 0.8
    assert await enrich.qualifies(store, ev, "fresh")
    await _decided(store, "fresh", 30, junk=True)
    quality.reset()
    assert not await enrich.qualifies(store, ev, "fresh")
    quality.reset()


@pytest.mark.parametrize("country,region,want", [
    ("US", "California", ("US", "California")),
    ("fj", "Central", ("FJ", "Central")),
    (None, "Singapore", (None, None)),
])
def test_parse_keeps_the_place_as_given(country, region, want):
    got = enrich.parse(_luna(country=country, region=region))
    assert (got["country"], got["region"]) == want
    assert got["summary"] == "Acme bought Beta on 1 Nov." and got["sentiment"] == "positive"


def test_prompt_nulls_country_for_several_countries():
    assert "two or more countries" in enrich.SYSTEM and "not where the" in enrich.SYSTEM


def test_parse_drops_malformed_fields():
    got = enrich.parse({"summary": "  A  b ", "sentiment": {"label": "great", "score": 9},
                        "country": "USA", "region": "Ohio"})
    assert got == {"summary": "A b", "sentiment": None, "score": None, "country": None,
                   "region": None}
    assert enrich.parse({"sentiment": {"label": "negative", "score": -3}})["score"] == -1.0


async def test_enriches_an_event_with_pinned_capped_requests(on):
    store = MemoryStore()
    ev = await _event(store, "confirmed")
    await enrich.discover(store, T0)
    assert await _run(store, on) == 1

    row = await store.get_enrichment(ev.id)
    assert (row.status, row.summary, row.sentiment, row.sentiment_score, row.country,
            row.region, row.confidence) == (
        "done", "Acme bought Beta on 1 Nov.", "positive", 0.5, "US", "California", "confirmed")
    assert len(store.embeddings[ev.id]) == 384
    assert row.cost_usd == pytest.approx(0.0002001)
    assert await store.enrich_spend(DAY) == pytest.approx(0.0002001)

    (chat,), (emb,) = (on.calls(p) for p in ("completions", "embeddings"))
    assert {p for p, _ in on.sent} == {"completions", "embeddings"}
    s = config.ENRICH
    assert chat["model"] == "openai/gpt-6-luna" and chat["reasoning"] == {"effort": "minimal"}
    assert chat["provider"] == {"allow_fallbacks": False, "max_price": {
        "prompt": s["MAX_PROMPT"], "completion": s["MAX_COMPLETION"]}}
    assert emb["provider"]["max_price"] == {"prompt": s["EMBED_MAX_PROMPT"]}
    assert product.enrichment_json(row) == {
        "summary": "Acme bought Beta on 1 Nov.", "sentiment": {"label": "positive", "score": 0.5},
        "country": "US", "region": "California"}

    assert await _run(store, on) == 0
    assert len(on.calls("completions")) == 1


async def test_null_country_is_stored_as_null(on):
    store = MemoryStore()
    ev = await _event(store)
    on.luna = _luna(country=None, region=None, label="negative", score=-0.6)
    await enrich.discover(store, T0)
    await _run(store, on)
    row = await store.get_enrichment(ev.id)
    assert (row.summary, row.country, row.region, row.sentiment, row.sentiment_score) == (
        "Acme bought Beta on 1 Nov.", None, None, "negative", -0.6)
    assert product.enrichment_json(row)["country"] is None


async def test_price_refusal_is_retried_later_then_fails(on, monkeypatch):
    monkeypatch.setitem(config.ENRICH, "MAX_ATTEMPTS", 2)
    store = MemoryStore()
    ev = await _event(store)
    on.fail["completions"] = (404, '{"error":{"message":"No endpoints found that satisfy the '
                                    'max price for this request"}}')
    await enrich.discover(store, T0)
    await _run(store, on)
    row = store.enrich_rows[ev.id]
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.run_ms == T0 + int(config.ENRICH["RETRY_S"] * 1000)
    assert "HTTP 404" in row.error
    assert await _run(store, on, T0 + 1000) == 0

    await _run(store, on, row.run_ms)
    row = store.enrich_rows[ev.id]
    assert (row.status, row.attempts) == ("failed", 2)
    assert ev.id not in store.embeddings


async def test_embedding_failure_on_the_last_attempt_keeps_the_answer(on, monkeypatch):
    monkeypatch.setitem(config.ENRICH, "MAX_ATTEMPTS", 1)
    store = MemoryStore()
    ev = await _event(store)
    on.fail["embeddings"] = (502, "bad gateway")
    await enrich.discover(store, T0)
    await _run(store, on)
    row = await store.get_enrichment(ev.id)
    assert (row.status, row.summary, row.country, row.sentiment) == (
        "done", "Acme bought Beta on 1 Nov.", "US", "positive")
    assert "HTTP 502" in row.error and ev.id not in store.embeddings


async def test_daily_cap_pauses_and_alarms(on, monkeypatch):
    store = MemoryStore()
    await _event(store)
    await enrich.discover(store, T0)
    await store.add_enrich_spend(DAY, config.ENRICH["DAILY_USD"])
    spend = enrich.Spend(store)
    errors = []
    monkeypatch.setattr(enrich.logger, "error", errors.append)
    assert await enrich.run_once(store, on.router(), spend, T0) == 0
    assert await enrich.run_once(store, on.router(), spend, T0 + 1) == 0
    assert on.sent == []
    assert len(errors) == 1 and errors[0].startswith("[ALARM] enrich_spend")
    (alarm,) = await ops._enrich_spend(store, T0)
    assert alarm["kind"] == "enrich_spend"
    next_day = T0 + 86_400_000
    assert await enrich.run_once(store, on.router(), spend, next_day) == 1


async def test_backfill_rows_wait_for_live_ones_and_their_own_cap(on):
    store = MemoryStore()
    old = await _event(store, publish_ms=T0 - 40 * 3_600_000)
    live = await _event(store)
    await enrich.discover(store, T0, since_ms=0, backfill=True, limit=1)
    await enrich.discover(store, T0)
    assert store.enrich_rows[old.id].backfill and not store.enrich_rows[live.id].backfill

    await store.add_enrich_spend(DAY, config.ENRICH["BACKFILL_DAILY_USD"])
    taken = await store.take_enrichments(5, enrich.LEASE_MS, T0, backfill=False)
    assert [r.event_id for r in taken] == [live.id]
    await store.defer_enrichment(live.id, 1, T0, None, count=False)
    taken = await store.take_enrichments(5, enrich.LEASE_MS, T0, backfill=True)
    assert [r.event_id for r in taken] == [live.id, old.id]


async def test_claiming_is_once_per_lease(on):
    store = MemoryStore()
    ev = await _event(store)
    await enrich.discover(store, T0)
    await enrich.discover(store, T0)
    assert len(store.enrich_rows) == 1
    a, b = await asyncio.gather(store.take_enrichments(5, enrich.LEASE_MS, T0, False),
                                store.take_enrichments(5, enrich.LEASE_MS, T0, False))
    assert len(a) + len(b) == 1
    (row,) = a or b
    assert await store.take_enrichments(5, enrich.LEASE_MS, T0 + 1000, False) == []

    (again,) = await store.take_enrichments(5, enrich.LEASE_MS, T0 + enrich.LEASE_MS + 1, False)
    assert again.attempts == row.attempts + 1
    assert not await store.finish_enrichment(EnrichRow(**{**row.__dict__, "status": "done"}))
    assert not await store.defer_enrichment(ev.id, row.attempts, T0, "late")
    assert await store.finish_enrichment(EnrichRow(**{**again.__dict__, "status": "done"}))


async def test_two_workers_call_each_event_once(on):
    store = MemoryStore()
    for _ in range(6):
        await _event(store)
    await enrich.discover(store, T0)
    spend = enrich.Spend(store)
    await asyncio.gather(*(enrich.run_once(store, on.router(), spend, T0) for _ in range(3)))
    await asyncio.gather(*(enrich.run_once(store, on.router(), spend, T0) for _ in range(3)))
    assert len(on.calls("completions")) == 6
    assert {r.status for r in store.enrich_rows.values()} == {"done"}


async def test_event_no_longer_qualifying_is_skipped_without_calls(on):
    store = MemoryStore()
    ev = await _event(store)
    await enrich.discover(store, T0)
    await store.update_event(ev.id, confidence="line")
    await _run(store, on)
    assert store.enrich_rows[ev.id].status == "skipped" and on.sent == []


def test_models_must_be_pinned(monkeypatch):
    enrich.check_settings()
    for slug in ("~openai/gpt-luna-latest", "openai/gpt-luna-latest", "gpt-6-luna"):
        monkeypatch.setitem(config.ENRICH, "MODEL", slug)
        with pytest.raises(RuntimeError):
            enrich.check_settings()
