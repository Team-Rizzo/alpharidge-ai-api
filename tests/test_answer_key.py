"""Answer key: adapters on saved feeds, matching, per-category Q, and staying private."""

import inspect
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.jobs.answer_key import Poller, poll
from app.main import app
from app.services.answer_key import accuracy, bea, bls, edgar, fed, match, releases
from app.services.answer_key.base import (
    EASTERN, FeedAdapter, FeedClient, actor, fact, feed_config, item,
)
from app.services.store import AccuracyCheckRow
from app.utils import hotkey_whitelist

from .pipeline import HEAD, HOUR_MS, T0, Lane

FIX = Path(__file__).parent / "fixtures" / "answer_key"
DAY_MS = 24 * HOUR_MS
LATER = T0 + 8 * DAY_MS
DEAL = [{"field": "deal_value", "value": 1.2, "unit": "usd_billions"}]


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_adapters_parse_saved_feeds():
    rows = edgar.parse(raw("edgar_search.json"))
    assert [(r.event_type, r.payload["stage"]) for r in rows] == [
        ("merger_acquisition", "completed"), ("personnel", None), ("ipo_listing", "announced")]
    assert rows[0].payload["hard_id"] == "edgar:0000950170-26-012345"
    assert rows[0].payload["actors"][0] == {"id_scheme": "cik", "id": "0000111111",
                                            "name": "Acme Inc.", "ticker": "ACME"}

    releases = bls.calendar(raw("bls.ics"))
    rows = bls.parse(raw("bls_latest.rss"), releases, datetime(2026, 10, 13, 20, tzinfo=EASTERN))
    by = {r.payload["facts"][0]["value"]: r for r in rows}
    assert by["Consumer Price Index (CPI)"].event_date == date(2026, 9, 11)
    assert by["Consumer Price Index (CPI)"].payload["facts"][1]["labels"] == {"measure": ["mom"]}
    assert by["Payroll Employment"].payload["facts"][1]["value"] == 29000.0

    rows = bea.records(bea.schedule(raw("bea_schedule.html")), "https://example.org/bea")
    assert [(r.payload["facts"][0]["value"], r.event_date) for r in rows] == [
        ("GDP", date(2026, 10, 29)), ("Personal income and outlays", date(2026, 10, 29)),
        ("Trade balance", date(2026, 11, 4))]


async def test_fed_poll_reads_the_statement():
    def serve(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        name, kind = (("fed_press.xml", "text/xml") if "feeds" in request.url.path
                      else ("fomc_statement.html", "text/html"))
        return httpx.Response(200, content=raw(name), headers={"content-type": kind})

    cfg = replace(feed_config(), public_only=False, domain_rps=1000)
    client = FeedClient(cfg, transport=httpx.MockTransport(serve))
    now = int(datetime(2026, 9, 17, tzinfo=timezone.utc).timestamp() * 1000)
    rows = await fed.Fed(client).poll(now)
    facts = {f["field"]: f for f in rows[0].payload["facts"]}
    assert rows[0].event_date == date(2026, 9, 16)
    assert facts["change_bps"]["value"] == 25 and facts["new_rate"]["alts"] == [3.75]
    await client.aclose()


class StaticClient:
    def __init__(self, body: bytes):
        self.body = body

    async def get(self, url):
        return self.body


async def test_bea_gives_release_times_only():
    adapter = bea.Bea(StaticClient(raw("bea_schedule.html")))
    release = datetime(2026, 10, 29, 8, 30, tzinfo=EASTERN).timestamp() * 1000
    assert await adapter.poll(int(release) + 60_000) == []
    times = [r.published_ms for r in adapter.scheduled(int(release) + 60_000)]
    assert int(release) in times


@pytest.mark.usefixtures("scoops_on")
async def test_scheduled_claim_needs_a_receipt_after_the_release_and_is_never_a_scoop(
        monkeypatch):
    monkeypatch.setattr(releases, "_book", releases.ReleaseBook())
    lane = Lane(check=True)
    await lane.sources()
    at = int(datetime(2026, 10, 29, 8, 30, tzinfo=EASTERN).timestamp() * 1000)
    await poll(lane.store, bea.Bea(StaticClient(raw("bea_schedule.html"))), at - DAY_MS)
    claim = dict(event_type="economic_data", event_date="2026-10-29", headline="GDP",
                 actors=[{"id_scheme": "wikidata", "id": "Q1443033",
                          "name": "Bureau of Economic Analysis"}],
                 facts=[{"field": "indicator", "value": "GDP"},
                        {"field": "actual", "value": 2.8, "unit": "percent"}])
    early = await lane.submit("m1", at - 600_000, source_url="https://lowblog.net/1", **claim)
    trusted = await lane.submit("m3", at - 500_000, source_url="https://newsone.com/1", **claim)
    await lane.step(at - 499_000)
    assert [lane.store.claims[r.id].reason for r in (early, trusted)] == ["pre_release"] * 2
    assert not lane.store.ledger
    late = await lane.submit("m2", at + 600_000, source_url="https://lowblog.net/2", **claim)
    await lane.step(at + 601_000)
    got = lane.store.claims[late.id]
    assert (got.status, got.reason) == ("rejected", "low_trust")
    assert not [r for r in lane.store.source_event_rows if r.role == "first"]
    index = b'<div class="infoHead">Accepted</div>\n<div class="info">2026-10-13 16:05:12</div>'
    assert edgar.accepted(index) == datetime(2026, 10, 13, 16, 5, 12, tzinfo=EASTERN)


def deal_item(value: float, n: int = 1, adapter: str = "bls"):
    return item(adapter, f"x-{n}", "merger_acquisition", date(2026, 10, 13), "Acme deal",
                "https://example.org/x", [actor("Acme Inc.", "wikidata", "Q1"),
                                          actor("Beta Systems", "wikidata", "Q2")],
                [fact("deal_value", value, "usd_billions")])


async def lane_with_event(**kw) -> Lane:
    lane = Lane(check=True)
    await lane.sources()
    await lane.submit("m1", T0 + 60_000, facts=DEAL, **kw)
    await lane.step(T0 + 61_000)
    return lane


async def run_match(lane: Lane, row, now_ms: int = T0 + 2 * HOUR_MS):
    (stored,) = await lane.store.add_answer_items([replace(row, observed_ms=now_ms)])
    status = await match.match_item(lane.store, stored, lane.entities, lane.places, now_ms, HEAD)
    return status, (await lane.store.answer_items(match.LIVE))[-1]


async def test_correct_match_raises_q_only_in_its_category():
    lane = await lane_with_event()
    before = {c: await accuracy.rating(lane.store, "m1", c, LATER) for c in ("finance", "politics")}
    status, stored = await run_match(lane, deal_item(1.2))
    assert status == match.MATCHED and stored.matched_claim_id is not None
    ev = await lane.store.get_event(stored.matched_event_id)
    assert ev.official_match
    assert await accuracy.rating(lane.store, "m1", "finance", LATER) > before["finance"]
    assert await accuracy.rating(lane.store, "m1", "politics", LATER) == before["politics"]


async def test_contradicted_claim_is_clawed_in_every_band():
    lane = await lane_with_event()
    status, stored = await run_match(lane, deal_item(3.5))
    claim = lane.store.claims[stored.matched_claim_id]
    assert status == match.CONFLICT and claim.trust_band == "prize-eligible"
    assert (claim.status, claim.wrong) == ("clawed_back", "answer_key")
    assert not [r for r in lane.store.ledger if r.kind == "penalty"]
    assert lane.paid()["m1"] == pytest.approx(0)
    assert any(e.role == "answer_key" for e in lane.store.source_event_rows)
    ev = await lane.store.get_event(stored.matched_event_id)
    assert not ev.official_match and ev.marked == "contradicted"

    low = Lane(check=True)
    await low.sources()
    row = await low.submit("m1", T0 + 60_000, facts=DEAL)
    await low.step(T0 + 61_000)
    assert low.store.claims[row.id].status == "provisional"
    await low.store.update_claim(row.id, "provisional", trust=0.05, trust_band="low")
    await run_match(low, deal_item(3.5))
    claim = low.store.claims[row.id]
    assert (claim.trust, claim.trust_band) == (0.05, "low")
    assert claim.status == "clawed_back" and claim.reason == match.CLAWED
    assert sum(low.paid().values()) == pytest.approx(0)
    (claw,) = [r for r in low.store.ledger if r.kind == "clawback"]
    assert claw.factors == {"trust": 0.05, "trust_band": "low"}


async def test_a_filing_conflict_counts_against_accuracy_but_never_claws():
    lane = await lane_with_event()
    status, stored = await run_match(lane, deal_item(3.5, adapter="edgar"))
    claim = lane.store.claims[stored.matched_claim_id]
    assert status == match.CONFLICT and claim.status == "provisional" and claim.wrong is None
    assert not [r for r in lane.store.ledger if r.kind in ("penalty", "clawback")]
    assert [c.correct for c in lane.store.check_rows.values()] == [False]


async def test_band_follows_the_fetched_source():
    lane = Lane(check=True)
    await lane.sources()
    lane.page("https://lowblog.net/x")
    fetch = lane.fetcher.fetch

    async def moved(url):
        page = await fetch("https://lowblog.net/x")
        return replace(page, url=url) if url == "https://newsone.com/r" else await fetch(url)

    lane.fetcher.fetch = moved
    row = await lane.submit("m1", T0 + 60_000, source_url="https://newsone.com/r")
    await lane.step(T0 + 61_000)
    claim = lane.store.claims[row.id]
    assert (claim.trust, claim.trust_band) == (0.1, "low")


async def test_answer_key_changes_points_only_through_q():
    plain, keyed = await lane_with_event(), await lane_with_event()
    await run_match(keyed, deal_item(1.2))
    for lane in (plain, keyed):
        await lane.settle(T0 + 73 * HOUR_MS)
    assert plain.paid() == keyed.paid() and plain.paid()["m1"] > 0


async def test_hotkeys_on_one_coldkey_share_q(monkeypatch):
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {"a1": "ck", "a2": "ck"})
    lane = await lane_with_event()
    await lane.submit("a1", T0, headline="Other deal", source_url="https://newsone.com/o")
    for i in range(5):
        await lane.store.add_accuracy_check(AccuracyCheckRow(
            claim_id=i, hotkey="a1", category="finance", kind=accuracy.ANSWER_KEY,
            correct=False, at_ms=T0, ref=str(i)))
    q = [await accuracy.rating(lane.store, hk, "finance", LATER) for hk in ("a1", "a2", "m1")]
    assert q[0] == q[1] < q[2]
    assert accuracy.q_value(0, 0) == accuracy.Q_PRIOR
    assert accuracy.q_value(100, 100) > accuracy.q_value(0, 0)
    assert accuracy.q_value(1000, 0) == accuracy.Q_MIN


class Saved(FeedAdapter):
    name, types = "edgar", ("merger_acquisition",)

    async def poll(self, now_ms):
        return edgar.parse(raw("edgar_search.json"))


async def test_poll_stores_recent_records_and_flags_gaps():
    lane = Lane(check=True)
    rows = edgar.parse(raw("edgar_search.json"))
    filed = int(datetime(2026, 10, 14, 1, tzinfo=timezone.utc).timestamp() * 1000)
    poller = Poller([Saved()])
    assert (await poller.run(lane.store, filed, HEAD)) == {"edgar": len(rows)}
    assert (await poller.run(lane.store, filed + 60_000, HEAD)) == {}
    later = filed + int(match.AK_GAP_H * HOUR_MS) + 60_000
    await match.match_open(lane.store, lane.entities, lane.places, later, HEAD)
    gaps = await match.gap_alerts(lane.store, later)
    assert {g["title"] for g in gaps} == {r.payload["title"] for r in rows}


def test_answer_key_is_not_served():
    for route in app.routes:
        source = inspect.getsource(inspect.getmodule(route.endpoint))
        assert "answer_key" not in source and "AnswerKey" not in source, route.path


@pytest.mark.parametrize("ref,got,want", [
    (fact("change_bps", 25.0, "basis_points"), (0.25, "percent"), True),
    (fact("actual", 29000.0, "count"), (29.0, "thousands"), True),
    (fact("actual", 0.4, "percent"), (0.9, "percent"), False),
    (fact("actual", 0.4, "percent"), (0.4, "usd"), None),
    (fact("score", "2-1", alts=["1-2"], exact=True), ("1 : 2", None), True),
])
def test_field_agreement(ref, got, want):
    from alpharidge_ai.schema import Fact

    assert match.agree(ref, Fact(field=ref["field"], value=got[0], unit=got[1])) is want


@pytest.mark.parametrize("facts,want", [
    ([("actual", 0.4, "percent"), ("measure", "mom", None)], True),
    ([("actual", 0.9, "percent"), ("measure", "mom", None)], False),
    ([("actual", 2.9, "percent"), ("measure", "yoy", None)], None),
    ([("actual", 2.9, "percent")], None),
])
def test_a_labelled_figure_compares_only_on_its_label(facts, want):
    from alpharidge_ai.schema import Fact

    payload = {"facts": [fact("indicator", "CPI", exact=True),
                         fact("actual", 0.4, "percent", labels={"measure": ["mom"]})],
               "guards": ["indicator"]}
    got = [Fact(field=f, value=v, unit=u) for f, v, u in facts]
    assert match.compare(payload, got) is want


async def test_a_bls_figure_finds_the_event_under_the_name_the_miner_used():
    lane = Lane(check=True)
    await lane.sources()
    rows = bls.parse(raw("bls_latest.rss"), bls.calendar(raw("bls.ics")),
                     datetime(2026, 10, 13, 20, tzinfo=EASTERN))
    cpi = next(r for r in rows if r.external_id.startswith("cpi|"))
    claim = await lane.submit(
        "m1", T0, event_type="economic_data", event_date=cpi.event_date.isoformat(),
        headline="CPI", source_url="https://newsone.com/cpi",
        actors=[{"id_scheme": "wikidata", "id": "Q2928428", "name": "Bureau of Labor Statistics"}],
        facts=[{"field": "indicator", "value": "CPI"},
               {"field": "actual", "value": 0.6, "unit": "percent"},
               {"field": "measure", "value": "mom"}])
    await lane.step(T0 + 1_000)
    assert lane.store.claims[claim.id].status == "provisional"
    status, _ = await run_match(lane, cpi)
    got = lane.store.claims[claim.id]
    assert status == match.CONFLICT and (got.status, got.wrong) == ("clawed_back", "answer_key")
    assert not any(r.kind == "penalty" for r in lane.store.ledger)


@pytest.mark.parametrize("value,want", [(None, "('edgar', 'fed', 'bls', 'bea')"), ("", "()"),
                                        ("bls", "('bls',)")])
def test_ak_adapters_env(value, want):
    env = {k: v for k, v in os.environ.items() if k != "EL_AK_ADAPTERS"}
    if value is not None:
        env["EL_AK_ADAPTERS"] = value
    out = subprocess.run([sys.executable, "-c", "from app.config import EVENT_LANE; "
                          "print(EVENT_LANE['AK_ADAPTERS'])"], env=env, capture_output=True,
                         text=True, cwd=Path(__file__).parent.parent)
    assert out.stdout.strip() == want, out.stderr[-300:]
