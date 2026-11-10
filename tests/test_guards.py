"""Design guard tests: splitting never pays more, rejected claims earn nothing on any route,
the miner's labels never raise pay, and each claim is judged at most once."""

from datetime import date

import pytest

from alpharidge_ai.mechanism.pools import window_points
from alpharidge_ai.mechanism.settlement import settle
from alpharidge_ai.schema import Category, EventType, category_of
from app.config import EVENT_LANE
from alpharidge_ai.verify.check import verdict_hash
from app.routers import ledger as ledger_router
from alpharidge_ai.judge.client import ScriptedJudge
from app.services import budget, ledger, ops, points, scoops
from app.services.entities import EntityIndex, EntityRecord
from app.utils import hotkey_whitelist

from .pipeline import HOUR_MS, T0, Lane

START = T0 + 40 * 60 * 1000


def actors(*qids):
    return [{"id_scheme": "wikidata", "id": q, "name": f"Company {q}",
             "role": "target" if q == qids[-1] else "acquirer"} for q in qids]


async def every_route(lane: Lane, scan: bool = False) -> Lane:
    await lane.sources()
    t = START
    ev = await lane.submit("m1", t, stage="announced", source_url="https://newsone.com/e1")
    await lane.step(t + 1000)
    event_id = str(lane.event_of(ev)) if lane.event_of(ev) else "0"
    await lane.submit("m2", t + 10_000, kind="stage", stage="approved", event_id=event_id,
                      source_url="https://wiretwo.org/s1")
    await lane.submit("m3", t + 20_000, kind="fact", event_id=event_id,
                      facts=[{"field": "deal_value", "value": 1.2, "unit": "usd_billions"}],
                      source_url="https://dailythree.net/f1")
    await lane.step(t + 21_000)

    scoop = dict(headline="Gamma to buy Delta", actors=actors("Q3", "Q4"))
    await lane.submit("m4", t + 30_000, source_url="https://lowblog.net/s", **scoop)
    await lane.step(t + 31_000)
    lane.page("https://newsone.com/c", published_ms=t + 40_000)
    await lane.submit("m5", t + 60_000, source_url="https://newsone.com/c", **scoop)
    await lane.step(t + 61_000)
    if scan:
        (held,) = [c for c in lane.store.claims.values() if c.hotkey == "m4"]
        await found(lane, held, "https://wiretwo.org/c", t + 40_000, t + 62_000,
                    deal_page("Company Q3", "Company Q4"))
        await lane.step(t + 63_000)

    await lane.settle(t + 73 * HOUR_MS)
    await lane.settle(t + 146 * HOUR_MS)
    return lane


@pytest.mark.parametrize("scoops", [False, True])
async def test_every_route_pays_when_checks_pass(scoops, monkeypatch):
    monkeypatch.setitem(EVENT_LANE, "SCOOPS", scoops)
    lane = await every_route(Lane(check=True), scan=scoops)
    routes = lane.by_route()
    assert set(routes) == {"event", *(("scoop",) if scoops else ())}
    assert all(v > 0 for v in routes.values()), routes
    off = [c for c in lane.store.claims.values() if c.hotkey in ("m2", "m3")]
    assert [c.status for c in off] == ["rejected"] * 2
    (low,) = [c for c in lane.store.claims.values() if c.hotkey == "m4"]
    (high,) = [c for c in lane.store.claims.values() if c.hotkey == "m5"]
    if scoops:
        assert (low.status, high.reason) == ("settled", "known_event")
    else:
        assert (low.status, low.reason) == ("rejected", "scoop_limit")


async def test_with_scoops_off_an_untrusted_page_waits_and_a_trusted_one_wins_in_full(
        monkeypatch):
    assert EVENT_LANE["SCOOPS"]
    monkeypatch.setitem(EVENT_LANE, "SCOOPS", False)
    lane = Lane(check=True)
    await lane.sources()
    scoop = dict(headline="Gamma to buy Delta", actors=actors("Q3", "Q4"))
    low = await lane.submit("m4", START, source_url="https://lowblog.net/s", **scoop)
    await lane.step(START + 1000)
    low = lane.store.claims[low.id]
    assert (low.status, low.reason) == ("rejected", "scoop_limit")
    assert lane.panel.judge.calls == 0
    win = await lane.submit("m5", START + 60_000, source_url="https://newsone.com/c", **scoop)
    await lane.step(START + 61_000)
    await lane.settle(START + 73 * HOUR_MS)
    win = lane.store.claims[win.id]
    assert (win.status, win.route, win.event_id) == ("settled", "event", low.event_id)
    full = points.factors("merger_acquisition", "event", win.trust, 1.0).product
    assert lane.paid() == {"m5": pytest.approx(full)}


async def test_reject_everything_pays_zero():
    lane = await every_route(Lane(check=False))
    assert not any(r.points > 0 for r in lane.store.ledger)
    assert sum(lane.paid().values()) == 0
    assert all(c.status == "rejected" for c in lane.store.claims.values())


async def operator_stream(k: int) -> Lane:
    """One operator's claims spread round-robin over k hotkeys, racing a rival, one round per
    epoch; in the last epoch a claim paid four epochs earlier is clawed back."""
    lane = Lane(check=True)
    await lane.sources()
    await lane.submit("rival", T0, headline="Warm-up", actors=actors("Q99"),
                      source_url="https://wiretwo.org/warm")
    await lane.step(T0 + 1000)
    n = 0

    def uid():
        nonlocal n
        n += 1
        return f"op{n % k}"

    for i in range(6):
        lane.head += EVENT_LANE["BLOCK_LENGTH"]
        t = START + i * 60_000
        ev = dict(headline=f"Deal {i}", actors=actors(f"Q{10 + i}", f"Q{30 + i}"),
                  stage="announced")
        await lane.submit(uid(), t, source_url=f"https://newsone.com/op{i}", **ev)
        if i % 2 == 0:
            await lane.submit("rival", t, source_url=f"https://wiretwo.org/rv{i}", **ev)
        await lane.step(t + 1000)
        other = dict(headline=f"Other deal {i}", actors=actors(f"Q{50 + i}", f"Q{70 + i}"))
        await lane.submit(uid(), t + 10_000, source_url=f"https://dailythree.net/opf{i}", **other)
        await lane.step(t + 21_000)
        if i == 5:
            clawed = next(c for c in lane.store.claims.values() if c.source_url.endswith("op1"))
            await ledger.clawback(lane.store, [clawed], "clawed:changed", lane.head)
    lane.head += EVENT_LANE["BLOCK_LENGTH"]
    await lane.settle(START + 73 * HOUR_MS)
    return lane


def _ops(values):
    return sum(v for hk, v in values.items() if hk.startswith("op"))


@pytest.mark.parametrize("k,coldkeys", [(2, 1), (5, 1), (5, 5)])
async def test_split_never_pays_more(k, coldkeys, monkeypatch):
    # One operator: its hotkeys sit on one coldkey, or on one coldkey each.
    monkeypatch.setattr(hotkey_whitelist, "coldkey_of",
                        lambda hk: f"op{int(hk[2:]) % coldkeys}" if hk.startswith("op") else hk)
    one, split = await operator_stream(1), await operator_stream(k)
    assert _ops(one.paid()) > 0
    assert any(r.kind == ledger.CLAWBACK for r in split.store.ledger)
    assert len({r.epoch for r in split.store.ledger}) > 1
    assert _ops(split.paid()) <= _ops(one.paid()) + 1e-9
    for lane in (one, split):
        assert len({hk for hk in lane.paid() if hk.startswith("op")}) <= (1 if lane is one else k)
    for capacity in (5.0, 1e6):
        shares = [settle(window_points([ledger.leaf(r) for r in lane.store.ledger]),
                         capacity).shares for lane in (one, split)]
        # Per-hotkey points round down, so only float division error is left.
        assert _ops(shares[1]) <= _ops(shares[0]) + 1e-12


DEAL = ("Acme Inc. agreed on Tuesday to acquire Beta Systems in a deal valued at $1.2 billion. "
        "The companies said the boards of both firms have approved the terms. The deal is "
        "expected to close next year, subject to the usual conditions for a deal this size.")
URL = "https://newsone.com/deal"


class TypeJudge(ScriptedJudge):
    """Says the page reports an event only of the deal's type."""

    def checks(self, claim, text):
        out = super().checks(claim, text)
        return {**out, "event": "yes" if claim.event_type.value == "merger_acquisition"
                else "no"}


async def pay_for(**kw) -> float:
    lane = Lane(check=None, judge=TypeJudge())
    await lane.sources()
    lane.page(URL, DEAL, published_ms=T0)
    await lane.submit("m1", START, source_url=URL, actors=[
        {"id_scheme": "wikidata", "id": "Q1", "role": "acquirer", "name": "Acme Inc."},
        {"id_scheme": "wikidata", "id": "Q2", "role": "target", "name": "Beta Systems"}],
        facts=[{"field": "deal_value", "value": 1.2, "unit": "usd_billions"}],
        quote=DEAL[:DEAL.index(" The companies")], **kw)
    await lane.step(START + 1000)
    await lane.settle(START + 73 * HOUR_MS)
    return sum(lane.paid().values())


async def test_labels_never_raise_pay_and_category_is_ignored():
    baseline = await pay_for()
    assert baseline > 0
    for cat in Category:
        if cat is not Category.FINANCE:
            assert await pay_for(category=cat.value) == baseline, cat
    for et in EventType:
        if et is EventType.MERGER_ACQUISITION:
            continue
        assert await pay_for(event_type=et.value,
                             category=category_of(et).value) <= baseline, et


# Page shapes the page judge reads.
REPLAY = (
    "{a} agreed on Tuesday to acquire {b} in a deal valued at $1.2 billion.",
    "{a} is reportedly in talks to acquire {b}, people familiar with the matter said.",
    "{a} denied that it had agreed to acquire {b}.",
    "{a} and {b} held a joint meeting with investors on Tuesday.",
    "{a} agreed to acquire {b} last year, the companies said.",
)


async def test_each_claim_is_judged_at_most_once():
    lane = Lane(check=None)
    await lane.sources()
    n = 100
    for i in range(n):
        a, b = f"Company Q{2 * i + 10}", f"Company Q{2 * i + 11}"
        url = f"https://newsone.com/replay/{i}"
        text = REPLAY[i % len(REPLAY)].format(a=a, b=b)
        lane.page(url, text, published_ms=START - 60_000)
        await lane.submit(f"m{i % 50}", START + i * 300, source_url=url, quote=text,
                          actors=actors(f"Q{2 * i + 10}", f"Q{2 * i + 11}"))
    for _ in range(n // 500 + 1):
        await lane.step(START + n * 300 + 1000)
    assert not any(c.status in ("queued", "novelty") for c in lane.store.claims.values())
    assert 0 < len(lane.panel.items) <= n
    assert len({i.claim_id for i in lane.panel.items}) == len(lane.panel.items)


async def found(lane: Lane, scoop, url: str, published_ms: int, at: int, text=None):
    """A validator's scan finds `url` for the scoop at `at`."""
    lane.page(url, text, published_ms=published_ms)
    out = await scoops.submit(lane.store, scoop.id, url, "v1", at)
    await scoops.run_candidates(lane.store, scoop.id, at, lane.verifier, lane.resolver,
                                lane.head)
    return out, lane.store.claims[scoop.id]


def deal_page(a: str, b: str, value: float = 1.2) -> str:
    return (f"{a} agreed on Tuesday to acquire {b} in a deal valued at ${value} billion. "
            "The boards of both companies approved the terms, the companies said.")


@pytest.mark.usefixtures("scoops_on")
async def test_a_scoop_is_paid_once_a_trusted_page_confirms_it():
    lane = Lane(check=True)
    await lane.sources()

    async def held(name, t, facts=()):
        kw = dict(headline=f"{name} deal", actors=actors(f"Q{name}1", f"Q{name}2"),
                  facts=list(facts))
        s = await lane.submit("m4", t, source_url=f"https://lowblog.net/{name}", **kw)
        await lane.step(t + 1000)
        url = f"https://newsone.com/{name}"
        lane.page(url, published_ms=t)
        c = await lane.submit("m5", t + 60_000, source_url=url, **kw)
        await lane.step(t + 61_000)
        assert (lane.store.claims[s.id].status, lane.store.claims[c.id].status) == (
            "verified", "fetched")
        return s, c

    # Confirmed by a page published before the scoop's receipt: first in, paid the tier price.
    text = deal_page("Company QA1", "Company QA2")
    s, c = await held("A", START)
    _, first = await found(lane, s, "https://wiretwo.org/A", START - 1000, START + 70_000, text)
    assert (first.status, first.route) == ("provisional", "scoop")
    (row,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    assert row.factors["base"] == points.price("merger_acquisition")
    await lane.step(START + 80_000)
    assert lane.store.claims[c.id].reason == "known_event"

    deal = [{"field": "deal_value", "value": 2.5, "unit": "usd_billions"}]
    s, c = await held("B", START + 100_000, deal)
    lane.panel.judge.script = {"wiretwo.org/B": {
        "fields": [{"field": "deal_value", "verdict": "no"}]}}
    _, wrong = await found(lane, s, "https://wiretwo.org/B", START + 150_000, START + 170_000,
                           deal_page("Company QB1", "Company QB2", 2.5))
    assert (wrong.reason, wrong.wrong) == ("scoop_mismatch", None)
    await lane.step(START + 180_000)
    assert lane.store.claims[c.id].status == "provisional"
    alarms = await ops.alarms(lane.store, START + 200_000, None)
    assert [a["claim_id"] for a in alarms["alarms"]
            if a["kind"] == "trusted_contradiction"] == [str(wrong.id)]

    deal = [{"field": "deal_value", "value": 1.2, "unit": "usd_billions"}]
    s, c = await held("C", START + 300_000, deal)
    url = "https://wiretwo.org/C"
    status, won = await found(lane, s, url, START + 350_000, START + 370_000,
                              deal_page("Company QC1", "Company QC2"))
    assert (status, won.status, won.route) == ("queued", "provisional", "scoop")
    record = ledger.judge_record(await lane.store.source_fetches(s.id))
    assert record["confirm"]["url"] == url and record["confirm"]["by"] == "v1"
    assert record["confirm"]["claim"]["source_url"] == url
    (row,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    assert row.factors["verdict"] == verdict_hash(record)
    t = record["confirm"]["trust"]
    assert row.factors["T"] == t == 0.8
    assert row.points == pytest.approx(ledger.PROVISIONAL_SHARE * points.factors(
        "merger_acquisition", "scoop", t, row.factors["A"]).product)
    assert [f.status for f in lane.store.fact_rows if f.claim_id == s.id] == ["accepted"]
    seen = {"confirm_content_hash": record["confirm"]["content_hash"]}
    assert await ledger_router.record_reverify(lane.store, s.id, "v2", 0, "agree", seen) \
        == "agree"
    assert await ledger_router.record_reverify(lane.store, s.id, "v2", 0, "agree",
                                               {**seen, "content_hash": "own edit"}) == "agree"
    assert await ledger_router.record_reverify(lane.store, s.id, "v2", 0, "agree",
                                               {"confirm_content_hash": "edited"}) == "changed"
    assert await found(lane, s, "https://dailythree.net/C", START + 351_000,
                       START + 371_000) == ("already_confirmed", won)
    await lane.step(START + 380_000)
    assert (lane.store.claims[c.id].status, lane.store.claims[c.id].reason) == (
        "rejected", "known_event")


@pytest.mark.usefixtures("scoops_on")
async def test_a_confirmed_scoop_s_own_page_is_not_rechecked():
    from app.services import recheck
    from .pipeline import Fetcher, Page

    lane = Lane(check=True)
    await lane.sources()
    url = "https://lowblog.net/D"
    lane.page(url, deal_page("Company QD1", "Company QD2", 0.7), published_ms=START - 60_000)
    s = await lane.submit("m4", START, source_url=url, headline="D deal",
                          actors=actors("QD1", "QD2"),
                          facts=[{"field": "deal_value", "value": 0.7, "unit": "usd_billions"}])
    await lane.step(START + 1000)
    _, won = await found(lane, s, "https://wiretwo.org/D", START + 50_000, START + 70_000,
                         deal_page("Company QD1", "Company QD2", 0.7))
    assert (won.status, won.route) == ("provisional", "scoop")

    now, again = START + 3 * HOUR_MS, Fetcher()
    fetched = []
    real = again.fetch

    async def fetch(u):
        fetched.append(u)
        return await real(u)

    again.fetch = fetch
    check = recheck.Recheck(fetcher=again)
    assert await check.run(lane.store, s.id, now) is False
    assert fetched == [] and lane.store.claims[s.id].status == "provisional"

    held = Lane(check=True)
    await held.sources()
    held.page(url, deal_page("Company QD1", "Company QD2", 0.7), published_ms=START - 60_000)
    h = await held.submit("m4", START, source_url=url, headline="D deal",
                          actors=actors("QD1", "QD2"))
    await held.step(START + 1000)
    again.pages[url] = Page(deal_page("Company QD1", "Company QD2", 0.702), START)
    await held.store.update_claim(h.id, "provisional")
    assert await recheck.Recheck(fetcher=again).run(held.store, h.id, now) is True


@pytest.mark.usefixtures("scoops_on")
async def test_a_ticker_scoop_is_confirmed_by_a_page_that_writes_the_company_name(monkeypatch):
    from alpharidge_ai.schema import Claim
    from alpharidge_ai.validator.judge import claim_of
    from alpharidge_ai.validator.reverify import stored_confirm
    from app.services import entities as entities_mod
    sec = entities_mod.SecNames()
    sec.update([("1494904", "GBLI", "Global Indemnity Group, LLC")])
    for k in ("names", "ciks", "tickers"):
        monkeypatch.setattr(entities_mod.SEC_NAMES, k, getattr(sec, k))
    lane = Lane(check=True)
    await lane.sources()
    gbli = [{"id_scheme": "other", "id": "GBLI", "name": "GBLI", "role": "acquirer"},
            actors("QH2")[0] | {"role": "target"}]
    s = await lane.submit("m4", START, headline="H deal", actors=gbli,
                          source_url="https://lowblog.net/H", quote="GBLI will acquire Company QH2.")
    await lane.step(START + 1000)
    text = deal_page("Global Indemnity Group, LLC", "Company QH2")
    _, won = await found(lane, s, "https://wiretwo.org/H", START + 50_000, START + 70_000, text)
    assert (won.status, won.route) == ("provisional", "scoop")
    confirm = ledger.judge_record(await lane.store.source_fetches(s.id))["confirm"]
    stored = Claim.model_validate(confirm["claim"])
    assert [(a.id, a.name) for a in stored.actors][0] == ("GBLI", "Global Indemnity Group, LLC")
    scoop = Claim.model_validate_json(lane.store.claims[s.id].body)
    assert stored_confirm(scoop, confirm) == stored
    (item,) = [i for i in lane.store.judge_item_rows.values() if i.kind == "confirm"]
    assert item.task["names"] == {"GBLI": "Global Indemnity Group, LLC"}
    assert claim_of(item.task, text) == stored


@pytest.mark.usefixtures("scoops_on")
async def test_a_misdated_scoop_fails_on_a_trusted_page_of_the_same_event():
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="G deal", actors=actors("QG1", "QG2"))
    s = await lane.submit("m4", START, source_url="https://lowblog.net/G", **kw)
    await lane.step(START + 1000)
    lane.page("https://newsone.com/G", published_ms=START)
    c = await lane.submit("m5", START + 60_000, source_url="https://newsone.com/G", **kw)
    await lane.step(START + 61_000)
    lane.panel.judge.script = {"wiretwo.org/G": {"timing": "no",
                                                 "not_news_reason": "old_event"}}
    _, failed = await found(lane, s, "https://wiretwo.org/G", START - 1000, START + 70_000,
                            deal_page("Company QG1", "Company QG2"))
    assert (failed.status, failed.reason) == ("rejected", "scoop_mismatch")
    await lane.step(START + 80_000)
    assert lane.store.claims[c.id].status == "provisional"


@pytest.mark.usefixtures("scoops_on")
async def test_a_scoop_expires_on_time_and_its_held_claim_goes_on():
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="E deal", actors=actors("QE1", "QE2"))
    s = await lane.submit("m4", START, source_url="https://lowblog.net/E", **kw)
    await lane.step(START + 1000)
    c = await lane.submit("m5", START + 60_000, source_url="https://newsone.com/E", **kw)
    await lane.step(START + 61_000)
    # Its page is checked against the scoop, and here does not confirm it.
    lane.panel.judge.script = {"newsone.com/E": {"event": "no"}}
    await scoops.run_candidates(lane.store, s.id, START + 62_000, lane.verifier, lane.resolver,
                                lane.head)
    await lane.step(START + 71 * HOUR_MS)
    assert lane.store.claims[c.id].status == "fetched"
    await lane.step(START + 72 * HOUR_MS)
    assert lane.store.claims[s.id].reason == "scoop_unconfirmed"
    await assert_debt(lane, lane.store.claims[s.id])
    await lane.step(START + 72 * HOUR_MS + 1000)
    c = lane.store.claims[c.id]
    assert (c.status, c.route) == ("provisional", "event")
    assert await found(lane, s, "https://wiretwo.org/E", START, START + 73 * HOUR_MS) == (
        "closed", lane.store.claims[s.id])


@pytest.mark.usefixtures("scoops_on")
async def test_a_trusted_claim_after_a_failed_scoop_takes_the_event():
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="G deal", actors=actors("QG1", "QG2"))
    s = await lane.submit("m4", START, source_url="https://lowblog.net/G", **kw)
    await lane.step(START + 1000)
    await lane.settle(START + 72 * HOUR_MS)
    s = lane.store.claims[s.id]
    assert s.reason == "scoop_unconfirmed"
    late = START + 80 * HOUR_MS
    c = await lane.submit("m5", late, source_url="https://newsone.com/G", **kw)
    await lane.step(late + 1000)
    c = lane.store.claims[c.id]
    assert (c.status, c.route, c.event_id) == ("provisional", "event", s.event_id)
    assert lane.store.events[s.event_id].first_claim_id == c.id


@pytest.mark.usefixtures("scoops_on")
async def test_a_hotkey_that_misses_its_scoops_is_turned_away_before_the_judge():
    lane = Lane(check=True)
    await lane.sources()
    for i in range(budget.SCOOP_ALLOW_MIN_JUDGED):
        s = await lane.submit("m4", START + i, headline=f"Miss {i}",
                              actors=actors(f"QM{i}", f"QN{i}"),
                              source_url=f"https://lowblog.net/m{i}")
    await lane.step(START + 1000)
    await lane.settle(START + 73 * HOUR_MS)
    calls = lane.panel.judge.calls
    later = await lane.submit("m4", START + 74 * HOUR_MS, headline="Later",
                              actors=actors("QL1", "QL2"), source_url="https://lowblog.net/l")
    other = await lane.submit("m6", START + 74 * HOUR_MS, headline="Other",
                              actors=actors("QO1", "QO2"), source_url="https://lowblog.net/o")
    await lane.step(START + 74 * HOUR_MS + 1000)
    assert lane.store.claims[s.id].reason == "scoop_unconfirmed"
    assert (lane.store.claims[later.id].reason, lane.store.claims[other.id].status) == (
        "scoop_limit", "verified")
    assert lane.panel.judge.calls == calls + 1


class StageJudge(ScriptedJudge):
    """Reads the page as reporting the deal called off."""

    def checks(self, claim, text):
        out = super().checks(claim, text)
        if claim.stage is not None and claim.stage.value == "cancelled":
            return out
        return {**out, "event": "no",
                "type_answer": {"kind": "merger_acquisition:cancelled", "p": 0.9}}


@pytest.mark.usefixtures("scoops_on")
async def test_a_trusted_page_that_reports_another_outcome_rejects_the_scoop():
    lane = Lane(check=True)
    await lane.sources()
    s = await lane.submit("m4", START, stage="completed", headline="Deal done",
                          actors=actors("QS1", "QS2"), source_url="https://lowblog.net/s")
    await lane.step(START + 1000)
    lane.panel.judge = StageJudge({"subject": "yes"})
    _, s = await found(lane, s, "https://wiretwo.org/off", START + 10_000, START + 20_000,
                       deal_page("Company QS1", "Company QS2"))
    assert s.reason == "scoop_mismatch"
    await assert_debt(lane, s)


async def assert_debt(lane: Lane, s) -> None:
    """The closed scoop's only row is its debt (`types.scoop_debt`)."""
    from datetime import datetime, timezone
    from alpharidge_ai.schema import Claim
    from alpharidge_ai.schema.types import scoop_debt

    def utc(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None

    (row,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    claim = Claim.model_validate_json(s.body)
    f = ledger.judged_fetch(await lane.store.source_fetches(s.id))
    assert (row.kind, row.route, row.category) == (ledger.DEBT, "scoop", s.category)
    assert row.points < 0 and row.points == scoop_debt(
        claim.event_type.value, claim.event_date, utc(s.received_ms), utc(f.published_ms))
    leaf = ledger.leaf(row)
    assert (leaf["validator_verdict"], leaf["categorical_key"]) == ("debt", f"{s.category}|scoop")


@pytest.mark.parametrize("check", [True, False])
async def test_company_page_is_primary_in_novelty_and_resolve(check):
    acme = EntityRecord("wikidata", "Q1", "Acme Inc.", "company", website="https://acme.com")
    lane = Lane(check=check, entities=EntityIndex([acme]))
    await lane.sources()
    row = await lane.submit("m1", START, source_url="https://acme.com/news/deal")
    await lane.step(START + 1000)
    got = lane.store.claims[row.id]
    assert (got.route, got.trust, got.trust_band) == ("event", 1.0, "primary")
    assert got.status == ("provisional" if check else "rejected")


def test_open_scoops_give_every_name_and_ticker_of_their_subjects(monkeypatch):
    from alpharidge_ai.schema import Claim
    from app.services import entities as entities_mod
    sec = entities_mod.SecNames()
    sec.update([("1494904", "GBLI", "Global Indemnity Group, LLC"),
                ("1331451", "FHLBC", "Federal Home Loan Bank of Chicago")])
    monkeypatch.setattr(scoops, "SEC_NAMES", sec)
    claim = Claim(kind="event", headline="Deal", event_type="merger_acquisition",
                  event_date="2026-11-02", source_url="https://lowblog.net/x",
                  actors=[{"id_scheme": "other", "id": "GBLI", "name": "GBLI",
                           "role": "acquirer"},
                          {"id_scheme": "cik", "id": "1331451", "name": "FHLBank Chicago",
                           "role": "target"}])
    names, tickers = scoops.subjects(claim, EntityIndex())
    assert set(names) == {"GBLI", "Global Indemnity Group, LLC", "FHLBank Chicago",
                          "Federal Home Loan Bank of Chicago"}
    assert tickers == ["GBLI", "FHLBC"]


@pytest.mark.usefixtures("scoops_on")
async def test_listed_validators_see_open_scoops_without_their_source(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import config
    from app.routers import claims as claims_router, scoops as scoops_router
    from app.utils import auth

    lane = Lane(check=True)
    await lane.sources()
    s = await lane.submit("m4", START, headline="F deal", actors=actors("QF1", "QF2"),
                          source_url="https://lowblog.net/F")
    await lane.step(START + 1000)
    monkeypatch.setattr(auth.auth_config, "enabled", False)
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"v1"})
    monkeypatch.setattr(scoops_router, "_now_ms", lambda: START + 2000)
    monkeypatch.setattr(scoops_router, "shared_index", lambda: lane.entities)
    app = FastAPI()
    app.include_router(scoops_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: lane.store
    client = TestClient(app)

    def signed(hk):
        return {"X-Auth-SS58Address": hk, "X-Auth-Signature": "00", "X-Auth-Message": "m",
                "X-Auth-Timestamp": "1"}

    assert client.get("/scoops/open", headers=signed("v2")).status_code == 403
    body = client.get("/scoops/open", headers=signed("v1")).json()
    (item,) = body["scoops"]
    assert item["id"] == s.id and set(item["subjects"]) >= {"Company QF2"}
    assert item["since"] < item["event_date"]
    assert not any(k in str(item) for k in ("lowblog", "m4", "quote"))
    assert isinstance(body["feeds"], list)
    post = {"scoop_id": s.id, "url": "https://wiretwo.org/F"}
    r = client.post("/scoops/confirm", json=post, headers=signed("v1"))
    assert (r.status_code, r.json()) == (202, {"status": "queued"})
    r = client.post("/scoops/confirm", json=post, headers=signed("v1"))
    assert (r.status_code, r.json()) == (200, {"status": "duplicate"})
    assert client.post("/scoops/confirm", json={**post, "scoop_id": 999},
                       headers=signed("v1")).status_code == 404
    assert client.post("/scoops/confirm", json=post, headers=signed("v2")).status_code == 403


@pytest.mark.usefixtures("scoops_on")
async def test_an_unread_scoop_is_held_on_its_receipt_and_paid_on_the_trusted_page():
    from alpharidge_ai.verify.check import UNREAD
    from .pipeline import Page

    lane = Lane(check=True)
    await lane.sources()

    async def unread(name, url, t, **kw):
        lane.fetcher.pages[url] = Page("", t - 60_000, unread=True)
        s = await lane.submit("m4", t, source_url=url, headline=f"{name} deal",
                              actors=actors(f"Q{name}1", f"Q{name}2"), **kw)
        await lane.step(t + 1000)
        return lane.store.claims[s.id]

    s = await unread("W", "https://lowblog.net/W", START, event_date="2026-10-10")
    assert (s.status, s.route) == ("verified", "scoop")
    assert lane.panel.items == []
    (f,) = await lane.store.source_fetches(s.id)
    assert f.check[UNREAD] and f.content_hash is None and ledger.unread_fetch(f)
    _, won = await found(lane, s, "https://wiretwo.org/W", START + 50_000, START + 70_000,
                         deal_page("Company QW1", "Company QW2"))
    assert (won.status, won.route) == ("provisional", "scoop")
    record = ledger.judge_record(await lane.store.source_fetches(s.id))
    assert record[UNREAD] is True and record["confirm"]["url"] == "https://wiretwo.org/W"
    (row,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    assert row.factors["verdict"] == verdict_hash(record)
    assert row.factors["base"] == points.SCOOP_PREMIUM * points.price("merger_acquisition")
    assert row.factors["A"] == round(points.age(date(2026, 10, 10), START), 6) < 1

    old = await unread("O", "https://lowblog.net/O", START + 1, event_date="2026-09-01")
    assert (old.status, old.reason) == ("rejected", "stale_date")
    trusted = await unread("T", "https://newsone.com/T", START + 2)
    assert (trusted.status, trusted.reason) == ("rejected", "blocked")
    gone = await lane.submit("m4", START + 3, source_url="https://lowblog.net/gone",
                             headline="G deal", actors=actors("QG1", "QG2"))
    lane.fetcher.pages.pop("https://lowblog.net/gone")
    for i in range(4):
        await lane.step(START + 1000 + i * 61_000)
    assert lane.store.claims[gone.id].reason == "fetch_failed"
    assert [i.task["kind"] for i in lane.panel.items] == ["confirm"]


@pytest.mark.usefixtures("scoops_on")
async def test_a_contested_scoop_is_paid_1x_and_flagged():
    lane = Lane(check=True)
    await lane.sources()
    s = await lane.submit("m4", START, source_url="https://lowblog.net/K", headline="K deal",
                          actors=actors("QK1", "QK2"))
    await lane.step(START + 1000)
    rival = await lane.submit("m6", START + 5000, source_url="https://lowblog.net/K2",
                              headline="K deal", actors=actors("QX9", "QK2"))
    other = await lane.submit("m6", START + 6000, source_url="https://lowblog.net/K3",
                              headline="K deal", actors=actors("QK1", "QK2"))
    for r in (rival, other):
        await lane.store.update_claim(r.id, "queued", status="verified", route="scoop",
                                      verified_ms=START + 7000)
    _, won = await found(lane, s, "https://wiretwo.org/K", START + 50_000, START + 70_000,
                         deal_page("Company QK1", "Company QK2"))
    assert won.status == "provisional"
    record = ledger.judge_record(await lane.store.source_fetches(s.id))
    assert record["confirm"]["contested"] == [str(rival.id)]
    (row,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    assert row.factors["base"] == points.price("merger_acquisition")
    flags = [t for t in await lane.store.trust_log() if t.field == "flag:contested"]
    assert [(t.new, t.reason) for t in flags] == [(str(s.id), f"conflicting scoops {rival.id}")]


@pytest.mark.usefixtures("scoops_on")
async def test_debt_rows_are_listed_with_their_signed_claim_and_net_on_the_coldkey():
    lane = Lane(check=True)
    await lane.sources()
    s = await lane.submit("m4", START, coldkey="ck", source_url="https://lowblog.net/N",
                          headline="N deal", actors=actors("QN1", "QN2"))
    paid = await lane.submit("m5", START, coldkey="ck", source_url="https://newsone.com/P",
                             headline="P deal", actors=actors("QP1", "QP2"))
    await lane.step(START + 1000)
    await lane.settle(START + 72 * HOUR_MS)
    s = lane.store.claims[s.id]
    await assert_debt(lane, s)
    (debt,) = [r for r in lane.store.ledger if r.kind == ledger.DEBT]
    (entry,) = [e for e in await ledger_router.settles(lane.store, lane.store.ledger)
                if "debt" in e]
    assert entry["ledger_id"] == str(debt.id)
    d = entry["debt"]
    assert (d["claim_id"], d["body"], d["reason"]) == (str(s.id), s.body, "scoop_unconfirmed")
    assert d["judge"] == ledger.judge_record(await lane.store.source_fetches(s.id))
    assert ledger.leaf(debt)["coldkey"] == "ck"
    earned = sum(r.points for r in lane.store.ledger if r.claim_id == paid.id)
    assert earned > 0 and sum(r.points for r in lane.store.ledger) == pytest.approx(
        earned + debt.points)
    await lane.settle(START + 80 * HOUR_MS)
    assert len([r for r in lane.store.ledger if r.kind == ledger.DEBT]) == 1


@pytest.mark.usefixtures("scoops_on")
async def test_an_unpaid_scoop_is_penalised_on_its_debt():
    from alpharidge_ai.schema.types import MISCONDUCT_PENALTY_X

    lane = Lane(check=True)
    await lane.sources()
    s = await lane.submit("m4", START, coldkey="ck", source_url="https://lowblog.net/Z",
                          headline="Z deal", actors=actors("QZ1", "QZ2"))
    await lane.step(START + 1000)
    now = START + 72 * HOUR_MS
    await lane.settle(now)
    s = lane.store.claims[s.id]
    (debt,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    done = await ops.penalise(lane.store, s, "hedge", "op", now, lane.head)
    assert done == {"clawed": False, "penalty": round(MISCONDUCT_PENALTY_X * debt.points, 6)}
    assert await ops.penalise(lane.store, s, "hedge", "op", now, lane.head) is None
    (pen,) = [r for r in lane.store.ledger if r.kind == ledger.PENALTY]
    assert (pen.claim_id, pen.route, pen.category, pen.coldkey) == (s.id, "scoop", s.category,
                                                                    "ck")
    (entry,) = [e for e in await ledger_router.settles(lane.store, lane.store.ledger)
                if e["ledger_id"] == str(pen.id)]
    assert (entry["paid"], entry["paid_epoch"]) == (ledger.leaf(debt), debt.epoch)
    (own,) = [e for e in await ledger_router.settles(lane.store, lane.store.ledger)
              if e["ledger_id"] == str(debt.id)]
    assert entry["debt"] == own["debt"] and entry["debt"]["body"] == s.body


OUTCOMES = {
    "ipo_listing": (dict(actors=[{"id_scheme": "wikidata", "id": "Q77", "role": "issuer",
                                  "name": "Company Q77"}]),
                    [dict(stage="announced", facts=[{"field": "exchange", "value": "NYSE"}]),
                     dict(stage="announced", facts=[{"field": "exchange", "value": "Nasdaq"}])]),
    "sanction_or_tariff": (
        dict(category="politics", place={"iso2": "US"},
             actors=[{"id_scheme": "wikidata", "id": "Q30", "role": "imposer",
                      "name": "United States"}]),
        [dict(stage="completed", facts=[{"field": "targets", "value": "Mexico"}]),
         dict(stage="cancelled", facts=[{"field": "targets", "value": "Mexico"}])]),
    "legislation_passed": (
        dict(category="politics", place={"iso2": "US"},
             actors=[{"id_scheme": "wikidata", "id": "Q11268", "role": "legislature",
                      "name": "United States Congress"}]),
        [dict(stage="completed", facts=[{"field": "bill", "value": "HR 1"}]),
         dict(stage="cancelled", facts=[{"field": "bill", "value": "HR 1"}])]),
}


@pytest.mark.usefixtures("scoops_on")
@pytest.mark.parametrize("event_type", list(OUTCOMES))
@pytest.mark.parametrize("days_apart", [0, 14])
async def test_opposite_scoops_on_open_ended_types(event_type, days_apart):
    """Inside the type's window the second is held on the same event, dated apart on its own;
    either way both are held and contest each other."""
    from datetime import date, timedelta
    from alpharidge_ai.schema import Claim

    lane = Lane(check=True)
    await lane.sources()
    common, sides = OUTCOMES[event_type]
    rows = []
    for i, side in enumerate(sides):
        day = date(2026, 10, 13) - timedelta(days=days_apart * i)
        s = await lane.submit(f"m{i}", START + i * 60_000, coldkey=f"ck{i}",
                              event_type=event_type, headline="Outcome",
                              event_date=day.isoformat(), quote="The officials said so on Tuesday.",
                              source_url=f"https://lowblog.net/{event_type}{i}", **common, **side)
        await lane.step(START + i * 60_000 + 1000)
        rows.append(lane.store.claims[s.id])
    first, second = rows
    assert (first.status, first.route) == ("verified", "scoop")
    assert (second.status, second.route) == ("verified", "scoop")
    assert (first.event_id == second.event_id) == (not days_apart)
    claim = Claim.model_validate(first.payload)
    assert await scoops.contested(lane.store, first, claim, lane.entities,
                                  START + 200_000) == [str(second.id)]


def test_a_hire_scooped_as_personnel_and_hedged_as_appointment_conflicts():
    from alpharidge_ai.schema import Claim
    from app.services import novelty
    from .pipeline import payload

    def personnel(person, role="ceo"):
        return Claim(**payload(event_type="personnel", headline="New chief", quote="Acme Inc. named its chief.",
                               actors=[{"id_scheme": "wikidata", "id": "Q1", "role": "company",
                                        "name": "Acme Inc."}],
                               facts=[{"field": "person", "value": person},
                                      {"field": "role", "value": role}]))

    def appointment(person, office="Chief Executive of Acme"):
        return Claim(**payload(event_type="appointment", category="politics", headline="Named",
                               quote="Acme Inc. named its chief.", place={"iso2": "US"},
                               actors=[{"id_scheme": "other", "id": person, "role": "appointee",
                                        "name": person}],
                               facts=[{"field": "office", "value": office}]))

    e = EntityIndex()
    assert novelty.conflicting(personnel("Ann Lee"), appointment("Bo Park"), e)
    assert novelty.conflicting(appointment("Bo Park"), personnel("Ann Lee"), e)
    assert not novelty.conflicting(personnel("Ann Lee"), appointment("Ann Lee"), e)
    assert not novelty.conflicting(personnel("Ann Lee"), appointment("Bo Park", "Mayor"), e)
    assert novelty.conflicting(personnel("Ann Lee"), personnel("Bo Park"), e)
    assert not novelty.conflicting(personnel("Ann Lee"), personnel("Bo Park", "cfo"), e)


@pytest.mark.parametrize("event_type,slot,a,b", [
    ("sanction_or_tariff", "targets", "Mexico", "Canada"),
    ("legislation_passed", "bill", "HR 1", "HR 2")])
async def test_tariffs_and_bills_are_one_event_only_for_the_same_targets_or_bill(
        event_type, slot, a, b):
    common, _ = OUTCOMES[event_type]
    lane = Lane(check=True)
    await lane.sources()

    async def send(i, facts):
        s = await lane.submit(f"m{i}", START + i * 60_000, event_type=event_type,
                              headline="Measure", stage="completed", facts=facts,
                              quote="The officials said so on Tuesday.",
                              source_url=f"https://newsone.com/{event_type}{i}", **common)
        await lane.step(START + i * 60_000 + 1000)
        return lane.store.claims[s.id]

    first = await send(0, [{"field": slot, "value": a}])
    other = await send(1, [{"field": slot, "value": b}])
    same = await send(2, [{"field": slot, "value": a}])
    bare = await send(3, [])
    assert (first.status, other.status) == ("provisional", "provisional")
    assert first.event_id != other.event_id
    assert (same.reason, same.event_id) == ("known_event", first.event_id)
    assert bare.reason == "known_event"


async def test_a_bill_by_its_number_and_by_its_name_is_one_event():
    common, _ = OUTCOMES["legislation_passed"]
    lane = Lane(check=True)
    await lane.sources()
    rows = []
    for i, bill in enumerate(("H.R. 5376", "Inflation Reduction Act", "HR 5377")):
        s = await lane.submit(f"m{i}", START + i * 60_000, event_type="legislation_passed",
                              headline="Measure", stage="completed",
                              facts=[{"field": "bill", "value": bill}],
                              quote="The officials said so on Tuesday.",
                              source_url=f"https://newsone.com/bill{i}", **common)
        await lane.step(START + i * 60_000 + 1000)
        rows.append(lane.store.claims[s.id])
    number, name, other = rows
    assert number.status == "provisional"
    assert (name.reason, name.event_id) == ("known_event", number.event_id)
    assert other.status == "provisional" and other.event_id != number.event_id


def test_a_vulnerability_keys_by_its_cve_id():
    from alpharidge_ai.schema import Claim
    from app.services import novelty
    from .pipeline import payload

    def vuln(cve):
        return Claim(**payload(event_type="vulnerability", category="technology",
                               headline="Flaw", quote="Acme Inc. patched a critical flaw.",
                               actors=[{"id_scheme": "wikidata", "id": "Q1", "role": "vendor",
                                        "name": "Acme Inc."}],
                               facts=[{"field": "cve_id", "value": cve}]))

    e = EntityIndex()
    keys = {c: novelty.same_keys(vuln(c), "cve_id", e)
            for c in ("CVE-2026-0042", "cve 2026 0042", "CVE-2026-1234")}
    assert "cve:2026-42" in keys["CVE-2026-0042"] and "cve:2026-42" in keys["cve 2026 0042"]
    assert not novelty._differ(keys["CVE-2026-0042"], keys["cve 2026 0042"])
    assert novelty._differ(keys["CVE-2026-0042"], keys["CVE-2026-1234"])


async def test_one_appointment_with_its_post_named_two_ways_is_one_event():
    lane = Lane(check=True)
    await lane.sources()
    rows = []
    for i, office in enumerate(("Secretary of State", "U.S. Secretary of State")):
        s = await lane.submit(f"m{i}", START + i * 60_000, event_type="appointment",
                              category="politics", headline="Named", stage="announced",
                              place={"iso2": "US"}, facts=[{"field": "office", "value": office}],
                              actors=[{"id_scheme": "other", "id": "Ann Lee", "role": "appointee",
                                       "name": "Ann Lee"}],
                              quote="The officials said so on Tuesday.",
                              source_url=f"https://newsone.com/post{i}")
        await lane.step(START + i * 60_000 + 1000)
        rows.append(lane.store.claims[s.id])
    first, again = rows
    assert first.status == "provisional"
    assert (again.reason, again.event_id) == ("known_event", first.event_id)


@pytest.mark.usefixtures("scoops_on")
@pytest.mark.parametrize("same_coldkey", [False, True])
async def test_a_conflicting_scoop_is_held_beside_the_first_and_the_confirmed_one_pays_1x(
        same_coldkey):
    lane = Lane(check=True)
    await lane.sources()

    async def scoop(hotkey, t, bidder, name):
        s = await lane.submit(hotkey, t, coldkey="ck" if same_coldkey else hotkey,
                              source_url=f"https://lowblog.net/{name}", headline="R deal",
                              actors=actors(bidder, "QR2"))
        await lane.step(t + 1000)
        return lane.store.claims[s.id]

    a = await scoop("m4", START, "QR1", "a")
    b = await scoop("m6", START + 10_000, "QX9", "b")
    again = await scoop("m7", START + 20_000, "QR1", "c")
    assert (a.status, b.status, b.route, b.event_id) == ("verified", "verified", "scoop",
                                                         a.event_id)
    assert (again.status, again.reason) == ("rejected", "known_event")
    listed = [i["id"] for i in await scoops.open_scoops(lane.store, lane.entities,
                                                         START + 30_000)]
    assert listed == [a.id, b.id]
    _, won = await found(lane, a, "https://wiretwo.org/R", START + 50_000, START + 70_000,
                         deal_page("Company QR1", "Company QR2"))
    assert won.status == "provisional"
    record = ledger.judge_record(await lane.store.source_fetches(a.id))
    assert record["confirm"]["contested"] == [str(b.id)]
    (row,) = [r for r in lane.store.ledger if r.claim_id == a.id]
    assert row.factors["base"] == points.price("merger_acquisition")
    listed = [i["id"] for i in await scoops.open_scoops(lane.store, lane.entities,
                                                         START + 75_000)]
    assert b.id not in listed and a.id not in listed
    assert await found(lane, b, "https://wiretwo.org/R2", START + 80_000, START + 90_000,
                       deal_page("Company QX9", "Company QR2")) == ("queued", b)
    late = await scoop("m8", START + 100_000, "QY7", "d")
    assert (late.status, late.reason) == ("rejected", "known_event")
    await lane.settle(START + 73 * HOUR_MS)
    b = lane.store.claims[b.id]
    assert b.reason == "scoop_unconfirmed"
    await assert_debt(lane, b)


@pytest.mark.usefixtures("scoops_on")
async def test_a_trusted_claim_on_a_held_scoop_confirms_it():
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="V deal", actors=actors("QV1", "QV2"))
    s = await lane.submit("m4", START, source_url="https://lowblog.net/V", **kw)
    await lane.step(START + 1000)
    lane.page("https://newsone.com/V", deal_page("Company QV1", "Company QV2"),
              published_ms=START + 30_000)
    c = await lane.submit("m5", START + 60_000, source_url="https://newsone.com/V", **kw)
    await lane.step(START + 61_000)
    (cand,) = await lane.store.scoop_candidates(s.id, (scoops.QUEUED,))
    assert (cand.url, cand.validator) == ("https://newsone.com/V", scoops.API_FINDER)
    await scoops.run_candidates(lane.store, s.id, START + 62_000, lane.verifier, lane.resolver,
                                lane.head)
    assert lane.store.claims[s.id].status == "provisional"
    await lane.step(START + 63_000)
    assert lane.store.claims[c.id].reason == "known_event"


async def test_control_characters_in_a_page_are_not_stored_and_a_failed_row_lets_it_move_on():
    from dataclasses import replace as copy

    lane = Lane(check=True)
    await lane.sources()
    real = lane.fetcher.fetch

    async def nul(url):
        return copy(await real(url), canonical_url=url + "\x00x", final_url=url + "\x01")

    lane.fetcher.fetch = nul
    s = await lane.submit("m1", START, source_url="https://newsone.com/nul")
    await lane.step(START + 1000)
    (f,) = await lane.store.source_fetches(s.id)
    assert (f.canonical_url, f.final_url) == ("https://newsone.com/nulx",
                                              "https://newsone.com/nul")

    store_put, calls = lane.store.add_source_fetch, []

    async def refuse(row):
        calls.append(row)
        if len(calls) == 1:
            raise ValueError("refused")
        return await store_put(row)

    lane.store.add_source_fetch = refuse
    t = await lane.submit("m1", START + 2000, source_url="https://newsone.com/other",
                          headline="Other deal", actors=actors("QN1", "QN2"))
    await lane.step(START + 3000)
    await lane.step(START + 4000)
    assert (lane.store.claims[t.id].status, lane.store.claims[t.id].reason) == (
        "rejected", "fetch_failed")


@pytest.mark.usefixtures("scoops_on")
async def test_a_failing_confirm_job_is_retried_then_dropped_so_the_scoop_closes(monkeypatch):
    import time
    from app.jobs import worker

    lane = Lane(check=True)
    await lane.sources()
    s = await lane.submit("m4", START, source_url="https://lowblog.net/J", headline="J deal",
                          actors=actors("QJ1", "QJ2"))
    await lane.step(START + 1000)
    assert await scoops.submit(lane.store, s.id, "https://wiretwo.org/J", "v1",
                               START + 2000) == "queued"
    runs = []

    async def broken(store, job):
        runs.append(job.attempts)
        raise RuntimeError("down")

    monkeypatch.setitem(worker.HANDLERS, scoops.CONFIRM_JOB, broken)
    pool, now = worker.JobPool(size=4, per_hotkey=2), time.time_ns() // 1_000_000
    for i in range(worker.JOB_MAX_ATTEMPTS + 1):
        await pool.poll(lane.store, now + i * 2 * worker.JOB_RETRY_S * 1000)
        await pool.drain()
    assert runs == [1, 2, 3]
    (cand,) = await lane.store.scoop_candidates(s.id, (scoops.REJECTED,))
    assert cand.reason == "check_failed"
    await lane.settle(START + 72 * HOUR_MS)
    assert lane.store.claims[s.id].reason == "scoop_unconfirmed"
    await assert_debt(lane, lane.store.claims[s.id])


@pytest.mark.usefixtures("scoops_on")
@pytest.mark.parametrize("bidder", ["QL1", "QX9"])
async def test_a_scoop_sent_after_a_slow_trusted_claim_does_not_hold_it(bidder):
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="L deal", actors=actors("QL1", "QL2"))
    c = await lane.submit("m5", START, source_url="https://newsone.com/L", **kw)
    lane.store.claims[c.id].status = "received"
    s = await lane.submit("m4", START + 60_000, source_url="https://lowblog.net/L",
                          headline="L deal", actors=actors(bidder, "QL2"))
    await lane.step(START + 61_000)
    assert lane.store.claims[s.id].status == "verified"
    lane.store.claims[c.id].status = "queued"
    await lane.step(START + 20 * 60_000)
    c = lane.store.claims[c.id]
    assert (c.status, c.route) == ("provisional", "event")
    assert lane.store.events[c.event_id].first_claim_id == c.id
    assert await lane.store.scoop_candidates(s.id, (scoops.QUEUED,)) == []
    await lane.settle(START + 73 * HOUR_MS)
    s = lane.store.claims[s.id]
    if bidder == "QL1":
        # Right but late: no miss and no debt.
        assert (s.status, s.reason) == ("rejected", "lost_line")
        assert [r for r in lane.store.ledger if r.claim_id == s.id] == []
        assert await lane.store.scoop_record(0, ["m4"]) in ({}, {"m4": (0, 0)})
    else:
        assert s.reason == "scoop_unconfirmed"
        await assert_debt(lane, s)


@pytest.mark.usefixtures("scoops_on")
async def test_later_trusted_reports_from_new_owners_make_a_confirmed_scoop_confirmed(
        monkeypatch):
    from app.services import intake, product, verifier

    lane = Lane(check=True)
    await lane.sources()
    monkeypatch.setattr(verifier, "_default", lane.verifier)
    kw = dict(headline="K deal", actors=actors("QK1", "QK2"))
    text = deal_page("Company QK1", "Company QK2")
    s = await lane.submit("m4", START, source_url="https://lowblog.net/K", **kw)
    await lane.step(START + 1000)
    _, won = await found(lane, s, "https://wiretwo.org/K", START + 30_000, START + 40_000, text)
    assert won.status == "provisional"
    ev = lane.store.events[won.event_id]
    assert await product.counted_owners(lane.store, ev) >= {"owner:b"}
    calls = []
    real = lane.fetcher.fetch

    async def counting(url):
        calls.append(url)
        return await real(url)

    lane.fetcher.fetch = counting

    async def report(hotkey, url, t):
        lane.page(url, text, published_ms=t - 1000)
        c = await lane.submit(hotkey, t, source_url=url, **kw)
        await lane.step(t + 1000)
        assert (lane.store.claims[c.id].status, lane.store.claims[c.id].reason) == (
            "rejected", "known_event")
        jobs = [j for j in lane.store.jobs.values()
                if j.kind == intake.REPORT_JOB and j.claim_id == c.id]
        for j in jobs:
            await verifier.report_job(lane.store, j)
        return len(jobs), lane.store.events[ev.id].confidence

    calls.clear()
    assert await report("m7", "https://lowblog.net/K2", START + 50_000) == (0, "reported")
    assert await report("m6", "https://wiretwo.org/K2", START + 60_000) == (1, "reported")
    assert calls == []
    assert await report("m5", "https://newsone.com/K", START + 70_000) == (1, "confirmed")
    assert calls == ["https://newsone.com/K"]
    assert [r.claim_id for r in lane.store.ledger] == [s.id]
