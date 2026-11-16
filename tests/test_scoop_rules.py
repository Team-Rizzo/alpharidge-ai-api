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
async def test_a_confirming_page_counts_at_its_sources_best_category():
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
