"""Verifier completion: the later re-check, the private settings and per-IP limits."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import config
from app.services import recheck, ops
from app.utils import rate_limit
from app.utils.rate_limit import TokenBuckets, limit
from tests.pipeline import HOUR_MS, T0, Fetcher, Fixed, Lane, Page
from tests.test_guards import actors
from tests.test_product import SEEN


async def test_later_recheck_by_trust_band(monkeypatch):
    monkeypatch.setitem(config.PRIVATE, "RECHECK_SAMPLE_RATE", 1.0)
    lane = Lane(check=True)
    await lane.sources()
    rows = []
    for i in range(3):
        rows.append(await lane.submit("m1", T0 + 60_000, source_url=f"https://newsone.com/{i}",
                                      headline=f"Deal {i}", actors=actors(f"Q{10 + i}",
                                                                          f"Q{30 + i}")))
        await lane.step(SEEN)
    jobs = [j for j in lane.store.jobs.values() if j.kind == recheck.JOB]
    assert sorted(j.claim_id for j in jobs) == [r.id for r in rows]
    lo, hi = config.PRIVATE["RECHECK_MIN_H"], config.PRIVATE["RECHECK_MAX_H"]
    assert all(SEEN + lo * HOUR_MS <= j.run_ms <= SEEN + hi * HOUR_MS for j in jobs)

    second = Fetcher()
    a, b, c = (r.source_url for r in rows)
    second.pages[a] = lane.fetcher.pages[a]
    second.pages[b] = Page("A different page.", T0)
    check = recheck.Recheck(fetcher=second, checker=Fixed(False, fetcher=second))
    now = SEEN + 2 * HOUR_MS
    assert [await check.run(lane.store, r.id, now) for r in rows] == [False, False, False]
    assert all(lane.store.claims[r.id].status == "provisional" for r in rows)
    flags = [t.reason for t in await lane.store.trust_log(actor="recheck")]
    assert flags.count("no longer backs the claim") == 1
    for r in rows[1:]:
        await lane.store.update_claim(r.id, "provisional", trust_band="low")
    assert [await check.run(lane.store, r.id, now) for r in rows[1:]] == [True, True]
    assert lane.store.claims[rows[1].id].reason == "clawed:changed"
    assert lane.paid()["m1"] > 0
    edited = recheck.Recheck(fetcher=second, checker=Fixed(True, fetcher=second))
    second.pages[a] = Page("An edited page that still says it.", T0)
    assert await edited.run(lane.store, rows[0].id, now) is False
    second.pages[a] = Page("ZZ-Notice-A: this story said it.", T0)
    assert await edited.run(lane.store, rows[0].id, now) is False
    await lane.store.update_claim(rows[0].id, "provisional", trust_band="low")
    assert await edited.run(lane.store, rows[0].id, now) is False
    got = lane.store.claims[rows[0].id]
    assert (got.status, got.wrong) == ("provisional", None)
    notices = [n for n in await ops._notices(lane.store, now) if n["claim_id"] == str(got.id)]
    assert sorted(n["kind"] for n in notices) == ["page_changed", "retraction_notice"]
    assert all(n["trust_band"] == "low" for n in notices)
    second.pages[a] = Page("An edited page that still says it.", T0)
    assert await edited.run(lane.store, rows[0].id, now) is True
    assert lane.store.claims[rows[0].id].reason == "clawed:changed"
    assert recheck.notice("zz-notice-b: this story misstated the value.")
    assert not recheck.notice(lane.fetcher.pages[a].text)


def test_private_settings_are_required(monkeypatch):
    assert config.missing_private() == []
    monkeypatch.setitem(config.PRIVATE, "RECHECK_SAMPLE_RATE", None)
    assert config.missing_private() == ["EL_RECHECK_SAMPLE_RATE"]


def test_token_bucket_and_429(monkeypatch):
    now = [0.0]
    b = TokenBuckets(rate=1, burst=2, clock=lambda: now[0])
    assert [b.take("ip") for _ in range(3)][:2] == [0, 0] and b.take("ip") > 0
    assert b.take("other") == 0
    now[0] += 1
    assert b.take("ip") == 0

    monkeypatch.setitem(rate_limit.RATES, "testx", (0.001, 1))
    app = FastAPI()
    app.get("/x", dependencies=[Depends(limit("testx"))])(lambda: {"ok": True})
    client = TestClient(app)
    assert client.get("/x").status_code == 200
    r = client.get("/x")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_a_recheck_proxy_must_keep_to_public_addresses(monkeypatch):
    from app.config import EVENT_LANE

    monkeypatch.setitem(EVENT_LANE, "RECHECK_PROXY", "http://127.0.0.1:3128")
    with pytest.raises(RuntimeError):
        recheck.make_fetcher()
    monkeypatch.setitem(EVENT_LANE, "RECHECK_PROXY_PUBLIC_ONLY", True)
    recheck.check_proxy()
    monkeypatch.setitem(EVENT_LANE, "RECHECK_PROXY", "")
    monkeypatch.setitem(EVENT_LANE, "RECHECK_PROXY_PUBLIC_ONLY", False)
    recheck.check_proxy()


def test_private_fetches_are_refused_outside_a_mock_network(monkeypatch):
    from app.config import EVENT_LANE
    from app.services import verifier

    monkeypatch.setitem(EVENT_LANE, "FETCH_PUBLIC_ONLY", False)
    with pytest.raises(RuntimeError):
        verifier.check_public_only(mock=False)
    verifier.check_public_only(mock=True)
    monkeypatch.setitem(EVENT_LANE, "FETCH_PUBLIC_ONLY", True)
    verifier.check_public_only(mock=False)
