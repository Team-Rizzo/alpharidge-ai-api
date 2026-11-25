"""Scoop pay by lead time under the "lead" pay table, as validators rebuild it."""

from datetime import datetime, timezone

import pytest

from alpharidge_ai.schema import Claim
from alpharidge_ai.schema import types as rules
from alpharidge_ai.validator.reverify import paid_right
from app import config
from app.services import ledger, points

from .pipeline import T0
from .test_guards import START, actors, deal_page, found
from .test_guards import Lane

NEW, LEAD = T0 - 3_600_000, START - 60_000


@pytest.fixture
def lead_on(monkeypatch):
    from app.config import EVENT_LANE
    monkeypatch.setitem(EVENT_LANE, "SCOOPS", True)
    for k in ("EL_EVENT_PAY_X", "EL_SCOOP_PREMIUM", "EL_SCOOP_MISS_DEBT_X"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rules, "_switch", None)
    monkeypatch.setenv("EL_PAY_TABLE", "new,lead")
    monkeypatch.setenv("EL_PAY_TABLE_FROM_MS", f"{NEW},{LEAD}")


def test_both_steps_are_served_in_order(lead_on):
    assert config.served_pay_switch() == [{"pay_table": "new", "from_ms": NEW},
                                          {"pay_table": "lead", "from_ms": LEAD}]
    assert rules.parse_pay_switch(config.served_pay_switch()) == rules.pay_switch()


async def _scoop(lane, name, t, source_url, lead_ms):
    s = await lane.submit("m4", t, source_url=source_url, headline=f"{name} deal",
                          actors=actors(f"Q{name}1", f"Q{name}2"))
    await lane.step(t + 1000)
    assert (lane.store.claims[s.id].status, lane.store.claims[s.id].route) == (
        "verified", "scoop")
    _, row = await found(lane, s, f"https://newsone.com/{name}", t + lead_ms,
                         t + lead_ms + 20_000, deal_page(f"Company Q{name}1", f"Company Q{name}2"))
    assert row.status == "provisional"
    (paid,) = [r for r in await lane.store.claim_ledger(s.id) if r.kind == ledger.PROVISIONAL]
    record = ledger.judge_record(await lane.store.source_fetches(s.id))
    item = {"route": "scoop", "points": paid.points, "factors": paid.factors,
            "judge": record, "published_at": None}
    received = datetime.fromtimestamp(row.received_ms / 1000, tz=timezone.utc)
    assert paid_right(item, Claim.model_validate_json(row.body), received)
    return paid.factors["base"] / points.price("merger_acquisition")


@pytest.mark.parametrize("name, lead_min, want", [
    ("A", 5, 0.25), ("B", 35, 5.125), ("C", 90, 10.0)])
async def test_a_confirmed_scoop_pays_by_its_lead_and_validators_agree(lead_on, name,
                                                                       lead_min, want):
    lane = Lane(check=True)
    await lane.sources()
    url = f"https://lowblog.net/{name}"
    lane.page(url, deal_page(f"Company Q{name}1", f"Company Q{name}2"),
              published_ms=START - 60_000)
    got = await _scoop(lane, name, START, url, lead_min * 60_000)
    assert got == pytest.approx(want)


async def test_a_scoop_citing_a_release_wire_pays_the_event_rate(lead_on):
    lane = Lane(check=True)
    await lane.sources()
    url = "https://www.stocktitan.net/news/QW/acme-to-acquire.html"
    lane.page(url, deal_page("Company QW1", "Company QW2"), published_ms=START - 60_000)
    assert await _scoop(lane, "W", START, url, 90 * 60_000) == pytest.approx(0.25)


async def test_before_the_lead_start_the_new_table_still_pays_the_full_premium(lead_on):
    lane = Lane(check=True)
    await lane.sources()
    url = "https://lowblog.net/P"
    lane.page(url, deal_page("Company QP1", "Company QP2"), published_ms=LEAD - 600_000)
    assert await _scoop(lane, "P", LEAD - 300_000, url, 12 * 60_000) == pytest.approx(10.0)


async def _unclear_first(lane, name, t):
    """A trusted page on the event, out before the scoop, that the judge could not decide on."""
    s = await lane.submit("m4", t, source_url=f"https://lowblog.net/{name}",
                          headline=f"{name} deal", actors=actors(f"Q{name}1", f"Q{name}2"))
    await lane.step(t + 1000)
    scoop = lane.store.claims[s.id]
    other = await lane.submit("m5", t - 120_000, source_url=f"https://newsone.com/{name}-early",
                              headline=f"{name} deal", actors=actors(f"Q{name}1", f"Q{name}2"))
    assert await lane.store.update_claim(other.id, lane.store.claims[other.id].status,
                                         status="rejected", reason="judge_unclear", trust=0.8,
                                         event_id=scoop.event_id)
    return scoop


async def test_a_trusted_page_the_judge_left_unclear_is_a_sighting_under_the_lead_table(lead_on):
    from app.services import scoops
    lane = Lane(check=True)
    await lane.sources()
    url = "https://lowblog.net/U"
    lane.page(url, deal_page("Company QU1", "Company QU2"), published_ms=START - 60_000)
    scoop = await _unclear_first(lane, "U", START)
    assert scoop.event_id is not None
    assert await scoops.first_seen(lane.store, scoop) == START - 120_000


async def test_before_the_lead_start_an_unclear_page_is_not_a_sighting(lead_on):
    from app.services import scoops
    lane = Lane(check=True)
    await lane.sources()
    t = LEAD - 300_000
    url = "https://lowblog.net/V"
    lane.page(url, deal_page("Company QV1", "Company QV2"), published_ms=t - 60_000)
    scoop = await _unclear_first(lane, "V", t)
    assert await scoops.first_seen(lane.store, scoop) is None
