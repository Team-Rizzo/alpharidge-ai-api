"""Scoop rules: fresh events and pages only, a cap on open scoops, confirm trust by category."""
import pytest

from app.config import EVENT_LANE
from app.services import budget, scoops
from app.services.store import SourceRow, SourceTrustRow

from .pipeline import HOUR_MS, Lane
from .test_guards import START, actors, deal_page, found


async def scoop(lane, hotkey, t, name, **kw):
    kw.setdefault("headline", f"{name} deal")
    s = await lane.submit(hotkey, t, source_url=f"https://lowblog.net/{name}",
                          actors=actors(f"Q{name}1", f"Q{name}2"), **kw)
    await lane.step(t + 1000)
    return lane.store.claims[s.id]


@pytest.mark.usefixtures("scoops_on")
async def test_a_stale_scoop_is_turned_away_before_the_judge_without_debt():
    lane = Lane(check=True)
    await lane.sources()
    calls = lane.panel.judge.calls
    old = await scoop(lane, "m4", START, "OLD", event_date="2026-10-10")
    lane.page("https://lowblog.net/PAGE", published_ms=START - 50 * HOUR_MS)
    page = await scoop(lane, "m4", START + 1, "PAGE")
    fresh = await scoop(lane, "m4", START + 2, "NEW")
    assert (old.reason, page.reason) == ("scoop_stale", "scoop_stale")
    assert (fresh.status, fresh.route) == ("verified", "scoop")
    assert lane.panel.judge.calls == calls + 1
    assert not [r for r in lane.store.ledger if r.claim_id in (old.id, page.id)]


@pytest.mark.usefixtures("scoops_on")
async def test_a_coldkey_holds_a_few_open_scoops_and_more_as_they_are_confirmed():
    lane = Lane(check=True)
    await lane.sources()
    base = EVENT_LANE["SCOOP_OPEN_BASE"]
    held = [await scoop(lane, f"h{i}", START + i, f"H{i}", coldkey="ck") for i in range(base + 1)]
    assert [s.status for s in held] == ["verified"] * base + ["rejected"]
    assert held[-1].reason == "scoop_limit"
    other = await scoop(lane, "m9", START + 100, "OTHER")
    assert other.status == "verified"
    _, won = await found(lane, held[0], "https://wiretwo.org/H0", START + 50_000, START + 70_000,
                         deal_page("Company QH01", "Company QH02"))
    assert won.status == "provisional"
    more = [await scoop(lane, f"h{i}", START + 80_000 + i, f"M{i}", coldkey="ck")
            for i in range(1 + budget.EVENT_LANE["SCOOP_OPEN_PER_HIT"] + 1)]
    assert [s.status for s in more] == ["verified"] * 3 + ["rejected"]
    assert budget.scoop_open_cap(100) == EVENT_LANE["SCOOP_OPEN_MAX"]


@pytest.mark.usefixtures("scoops_on")
async def test_a_confirming_page_counts_at_the_category_its_type_shares():
    lane = Lane(check=True)
    await lane.sources()
    src = await lane.store.add_source(SourceRow(domain="finpaper.com", owner_id="owner:f",
                                                status="active", base_trust=0.1,
                                                category_trust={"finance": 0.8}))
    await lane.store.put_source_trust([SourceTrustRow(src.id, "finance", base=0.8, trust=0.8)])
    s = await scoop(lane, "m4", START, "P", event_type="partnership", category="technology",
                    headline="P partnership")
    assert (s.status, s.route) == ("verified", "scoop")
    _, won = await found(lane, s, "https://finpaper.com/P", START + 50_000, START + 70_000,
                         deal_page("Company QP1", "Company QP2"))
    assert won.status == "provisional"
    (row,) = [r for r in lane.store.ledger if r.claim_id == s.id]
    assert row.factors["T"] == 0.8


async def test_staleness_counts_the_event_day_end_and_the_page():
    from datetime import datetime, timezone
    from alpharidge_ai.schema import Claim
    from .pipeline import payload

    claim = Claim(**payload(event_date="2026-10-13"))
    end = int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp() * 1000)
    assert not scoops.stale(claim, end + 48 * HOUR_MS, None)
    assert scoops.stale(claim, end + 48 * HOUR_MS + 1, None)
    page = datetime.fromtimestamp((end - HOUR_MS) / 1000, tz=timezone.utc)
    assert scoops.stale(claim, end + 47 * HOUR_MS + 1, page)
    assert not scoops.stale(claim, end + 47 * HOUR_MS, page)


@pytest.mark.usefixtures("scoops_on")
async def test_a_stale_scoop_counts_as_junk_and_uses_the_check_budget():
    from alpharidge_ai.mechanism.quality import JUNK

    lane = Lane(check=True)
    await lane.sources()
    old = await scoop(lane, "m4", START, "OLD", event_date="2026-10-10", coldkey="ck4")
    assert old.reason == "scoop_stale"
    assert await lane.store.quality_outcomes(0, JUNK) == [("ck4", False, old.id)]
    assert (await lane.store.budget_use("m4", 0))[0] == 1


async def test_confirm_trust_reaches_only_the_category_a_type_shares():
    from alpharidge_ai.schema import Claim, category_of
    from .pipeline import payload

    lane = Lane(check=True)
    fin = SourceRow(domain="finpaper.com", owner_id="owner:f", status="active", base_trust=0.1,
                    category_trust={"finance": 0.8}, id=1)
    tech = SourceRow(domain="techpaper.com", owner_id="owner:t", status="active",
                     base_trust=0.1, category_trust={"technology": 0.8}, id=2)

    async def at(source, event_type):
        claim = Claim(**payload(event_type=event_type,
                                category=category_of(event_type).value))
        ct, category = await scoops.category_trust(lane.store, lane.verifier, lane.entities,
                                                   claim, source, f"https://{source.domain}/a")
        return ct.t, category

    assert await at(fin, "partnership") == (0.8, "finance")
    assert await at(fin, "funding") == (0.8, "finance")
    assert await at(tech, "merger_acquisition") == (0.8, "technology")
    # A finance desk does not confirm sports, politics or a technical disclosure.
    assert await at(fin, "transfer") == (0.1, "sports")
    assert await at(fin, "election_result") == (0.1, "politics")
    assert await at(fin, "vulnerability") == (0.1, "technology")
    assert await at(tech, "monetary_policy") == (0.1, "finance")


def test_shared_categories_pair_company_news_and_policy_news_only():
    from alpharidge_ai.schema import EventType, category_of

    company = {"earnings", "guidance", "merger_acquisition", "bankruptcy", "personnel",
               "layoffs", "ipo_listing", "funding", "product_launch", "partnership",
               "infrastructure_investment", "security_incident"}
    assert set(scoops.SHARED_CATEGORY) == company | {"sanction_or_tariff", "tech_regulation"}
    for t, others in scoops.SHARED_CATEGORY.items():
        own = category_of(EventType(t)).value
        assert own not in others and len(set(others)) == len(others), t
        if t in company:
            assert {own, *others} == {"finance", "technology"}, t
    # A finance desk confirms a breach and a tariff; a politics desk a tech regulator's fine.
    assert scoops.confirm_categories("security_incident") == ("technology", "finance")
    assert scoops.confirm_categories("sanction_or_tariff") == ("politics", "finance")
    assert "politics" in scoops.confirm_categories("tech_regulation")


@pytest.mark.usefixtures("scoops_on")
async def test_checks_pass_scoops_over_the_cap_and_resolve_holds_only_up_to_it(monkeypatch):
    from alpharidge_ai.schema import Claim
    from app.services import line, novelty, trust, verifier

    lane = Lane(check=True)
    await lane.sources()
    base = EVENT_LANE["SCOOP_OPEN_BASE"]
    rows = [await lane.submit(f"h{i}", START + i, coldkey="ck", headline=f"C{i} deal",
                              source_url=f"https://lowblog.net/C{i}",
                              actors=actors(f"QC{i}1", f"QC{i}2")) for i in range(base + 3)]
    now = START + 1000
    for row in await line.ready_claims(lane.store, now):
        source = await trust.resolve(lane.store, row.source_url, now)
        ct = await trust.of_claim(lane.store, lane.verifier.registry, lane.entities,
                                  Claim.model_validate(row.payload), source, row.category,
                                  row.source_url)
        await novelty.apply(lane.store, row, lane.entities, lane.places, now, trust=ct.t)
    for row in await lane.store.claims_by_status("novelty"):
        await lane.verifier.fetch(lane.store, row, now_ms=now)
    monkeypatch.setattr(verifier, "_default", lane.verifier)
    monkeypatch.setitem(EVENT_LANE, "VERIFY_CONCURRENCY", 4)
    # The check only reads the open-scoop count: none is held until resolve, so every check of
    # a batch sees the same count and passes, as checks running side by side would.
    assert await verifier.verify_ready(lane.store, now) == base + 3
    assert {lane.store.claims[r.id].status for r in rows} == {"fetched"}
    # Resolve holds them one at a time, so the cap holds.
    await lane.resolver.run(lane.store, now, head=lane.head)
    out = [lane.store.claims[r.id] for r in rows]
    assert [c.status for c in out] == ["verified"] * base + ["rejected"] * 3
    assert {c.reason for c in out[base:]} == {"scoop_limit"}


@pytest.mark.usefixtures("scoops_on")
async def test_an_undated_scoop_page_fails_the_date_gate_before_it_is_held(monkeypatch):
    from dataclasses import replace
    from .pipeline import T0

    lane = Lane(check=None)
    await lane.sources()
    url = "https://lowblog.net/U"
    lane.page(url, "Acme Inc. will acquire Beta Systems, the companies said on Tuesday in a "
              "statement. The boards of both companies approved the terms.", published_ms=T0)
    served = lane.fetcher.fetch

    async def undated(u):
        return replace(await served(u), published_at=None)

    monkeypatch.setattr(lane.fetcher, "fetch", undated)
    calls = lane.panel.judge.calls
    s = await lane.submit("m4", START, source_url=url, event_date="2026-10-13")
    await lane.step(START + 1000)
    s = lane.store.claims[s.id]
    assert (s.status, s.reason) == ("rejected", "no_publish_date")
    (f,) = await lane.store.source_fetches(s.id)
    assert f.check["date_gate"] is False
    assert lane.panel.judge.calls == calls


@pytest.mark.usefixtures("scoops_on")
async def test_an_unread_scoop_with_a_fresh_date_on_an_old_story_is_never_confirmed():
    from .pipeline import Page

    lane = Lane(check=None)
    await lane.sources()
    url = "https://lowblog.net/OLD"
    lane.fetcher.pages[url] = Page("", START - 60_000, unread=True)
    # The story ran ten days earlier; the miner dates its event on the day it sends it.
    s = await lane.submit("m4", START, source_url=url, headline="OLD deal",
                          actors=actors("QO1", "QO2"), event_date="2026-10-13")
    await lane.step(START + 1000)
    assert (lane.store.claims[s.id].status, lane.store.claims[s.id].route) == (
        "verified", "scoop")
    _, held = await found(lane, s, "https://wiretwo.org/OLD", START - 10 * 24 * HOUR_MS,
                          START + 70_000, deal_page("Company QO1", "Company QO2"))
    assert held.status == "verified"
    (cand,) = await lane.store.scoop_candidates(s.id, (scoops.REJECTED,))
    assert cand.reason == "future_date"
    await lane.settle(START + 73 * HOUR_MS)
    s = lane.store.claims[s.id]
    assert (s.status, s.reason) == ("rejected", "scoop_unconfirmed")
    assert [r.route for r in lane.store.ledger if r.claim_id == s.id]
