import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import claims as claims_router, dashboard as dashboard_router, ledger as ledger_router
from alpharidge_ai.mechanism import quality as qm
from app.services import budget, intake, quality
from app.services.store import MemoryStore
from app.utils import hotkey_whitelist
from app.utils.clock import DAY_MS
from tests.test_store_contract import T0, claim


async def _judged(store, hk, ck, outcomes, at=T0):
    for i, (status, reason) in enumerate(outcomes):
        row = await store.create_claim(claim(hk, coldkey=ck))
        await store.update_claim(row.id, "received", status=status, reason=reason,
                                 judged_ms=at + i, decided_ms=at + i)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(quality, "_now_ms", lambda: T0 + DAY_MS)
    quality.reset()
    yield
    quality.reset()


async def test_outcomes_are_served_from_the_asked_start():
    store = MemoryStore()
    await _judged(store, "hk", "ck", [("provisional", None), ("rejected", "judge_reject"),
                                      ("rejected", "known_event")])
    app = FastAPI()
    app.include_router(ledger_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    app.dependency_overrides[ledger_router.get_validator_hotkey] = lambda: "vali"
    client = TestClient(app)
    body = client.get("/ledger/quality").json()
    ids = [c for _, _, c in body["outcomes"]]
    assert body["owners"] == ["ck"] and [r[:2] for r in body["outcomes"]] == [[0, 1], [0, 0]]
    # Only the starts validators and the default use are served: each other one would cost
    # a full read and a cache entry.
    assert client.get("/ledger/quality", params={"from_ms": T0 + 1}).status_code == 400
    assert client.get("/ledger/quality",
                      params={"from_ms": T0 + 1, "epoch": 7}).status_code == 400
    # An epoch's outcomes stop where its points were signed, whenever they are asked for.
    assert client.get("/ledger/quality", params={"epoch": 7}).status_code == 409
    await store.put_attestation(ledger_router.ledger.LEDGER_SIGNER, 7, {"capacity": 1.0})
    store.attestation_ms[(ledger_router.ledger.LEDGER_SIGNER, 7)] = T0 + 1
    await _judged(store, "hk", "ck", [("provisional", None)])
    body = client.get("/ledger/quality", params={"epoch": 7}).json()
    assert body["until_ms"] == T0 + 1 and [r[:2] for r in body["outcomes"]] == [[0, 1], [0, 1]]


async def test_the_dashboard_shows_each_coldkeys_standing():
    store = MemoryStore()
    await _judged(store, "spam", "ck-spam", [("rejected", "unsupported")] * 120)
    await _judged(store, "good", "ck-good", [("provisional", None)] * 300)
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    client = TestClient(app)
    body = client.get("/dashboard/quality").json()
    assert body["rule"]["half_life"] == 20 and body["rule"]["pay_full"] == 0.6
    got = {r["coldkey"]: r for r in body["coldkeys"]}
    assert got["ck-spam"]["pay_multiplier"] == 0 and got["ck-spam"]["multiplier"] == 0
    assert got["ck-spam"]["junk"] == 120
    assert got["ck-good"]["pay_multiplier"] == 1 and got["ck-good"]["paid"] == 300
    new = client.get("/dashboard/quality", params={"coldkey": "ck-new"}).json()["coldkeys"]
    assert new == [{"coldkey": "ck-new", "q": 0.5, "pay_multiplier": 0.8, "multiplier": 1.0,
                    "paid": 0, "junk": 0}]


async def test_a_clawed_back_claim_keeps_its_place_and_a_typeless_unclear_is_junk():
    from app.services.store import SourceFetchRow
    store = MemoryStore()
    await _judged(store, "hk", "ck", [("clawed_back", None), ("rejected", "judge_unclear"),
                                      ("rejected", "judge_unclear")])
    ids = sorted(store.claims)
    await store.add_source_fetch(SourceFetchRow(claim_id=ids[1], verdict="rejected",
                                                check={"judge": {"type_none": True}}))
    got = await quality.outcomes(store, T0)
    assert got == [("ck", True, ids[0]), ("ck", False, ids[1])]


async def test_the_budget_scales_by_quality_only_with_the_flag(monkeypatch):
    store = MemoryStore()
    await _judged(store, "spam", "ck-spam", [("rejected", "unsupported")] * 120)
    await _judged(store, "good", "ck-good", [("provisional", None)] * 30)
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {"spam": "ck-spam", "good": "ck-good"})
    monkeypatch.setitem(budget.EVENT_LANE, "CHECK_BUDGET_BASE", 200)
    base = await budget.limits(store, ["spam", "good", "new"], T0 + 1000)
    assert base["spam"] == 200 and base["new"] == 200
    monkeypatch.setitem(budget.EVENT_LANE, "QUALITY_BUDGET", True)
    got = await budget.limits(store, ["spam", "good", "new"], T0 + 1000)
    assert got == {"spam": 20, "good": base["good"], "new": 200}
    assert await budget.budget(store, "spam", T0 + 1000) == 20


async def test_an_epochs_answer_is_the_same_whenever_and_wherever_it_is_asked(monkeypatch):
    store = MemoryStore()
    clock = [T0]
    monkeypatch.setattr(intake, "_now_ms", lambda: clock[0])

    async def made(status, judged):
        row = await store.create_claim(claim("hk", coldkey="ck"))
        await store.update_claim(row.id, "received", status=status, judged_ms=judged)
        return await store.get_claim(row.id)

    async def to(t, row, status, **fields):
        clock[0] = t
        return await intake.transition(store, await store.get_claim(row.id), status, **fields)

    # A scoop passed and held at T0, a claim paid at T0+1, a junk reject at T0+2 and a
    # low-trust reject at T0+3; the epoch is signed at T0+10.
    scoop = await made("verified", T0)
    paid = await to(T0 + 1, await made("verified", T0 + 1), "provisional")
    junk = await to(T0 + 2, await made("fetched", T0 + 2), "rejected", reason="judge_reject")
    low = await to(T0 + 3, await made("fetched", T0 + 3), "rejected", reason="low_trust")
    first = await quality.outcomes(store, T0, T0 + 10)
    # After the cut: the scoop is paid, the paid claim settles and is clawed back, and the
    # low-trust claim goes back to its line and is paid. Another process asks.
    await to(T0 + 20, scoop, "provisional")
    await to(T0 + 21, paid, "settled")
    await to(T0 + 22, paid, "clawed_back")
    await store.update_claim(low.id, "rejected", status="fetched", reason=None)
    await to(T0 + 23, low, "verified")
    await to(T0 + 24, low, "provisional")
    quality.reset()
    second = await quality.outcomes(store, T0, T0 + 10)
    assert first == second == [("ck", True, paid.id), ("ck", False, junk.id)]
    assert qm.locked(first) == qm.locked(second)
    quality.reset()
    later = [c for _, _, c in await quality.outcomes(store, T0)]
    assert later == [paid.id, junk.id, scoop.id, low.id]


async def test_a_claim_is_stamped_once_when_paid_or_rejected(monkeypatch):
    store = MemoryStore()
    clock = [T0 + 5]
    monkeypatch.setattr(intake, "_now_ms", lambda: clock[0])
    row = await store.create_claim(claim("hk"))
    await store.update_claim(row.id, "received", status="verified")
    row = await intake.transition(store, await store.get_claim(row.id), "provisional")
    assert row.decided_ms == T0 + 5
    clock[0] = T0 + 9
    row = await intake.transition(store, row, "settled")
    row = await intake.transition(store, row, "clawed_back")
    assert row.decided_ms == T0 + 5
    other = await store.create_claim(claim("hk"))
    other = await intake.transition(store, other, "queued")
    assert other.decided_ms is None
    other = await intake.transition(store, other, "rejected", reason="budget_expired")
    assert other.decided_ms == T0 + 9


async def test_concurrent_reads_share_one_query():
    store = MemoryStore()
    await _judged(store, "hk", "ck", [("provisional", None)] * 3)
    calls, real = [], store.quality_outcomes

    async def slow(*args):
        calls.append(args)
        await asyncio.sleep(0.01)
        if len(calls) == 2:
            raise RuntimeError("db down")
        return await real(*args)

    store.quality_outcomes = slow
    got = await asyncio.gather(*(quality.outcomes(store) for _ in range(20)))
    assert len(calls) == 1 and all(g == got[0] for g in got) and len(got[0]) == 3
    # A failed read reaches every waiter and is not kept.
    quality.reset()
    got = await asyncio.gather(*(quality.outcomes(store) for _ in range(5)),
                               return_exceptions=True)
    assert len(calls) == 2 and all(isinstance(g, RuntimeError) for g in got)
    assert len(await quality.outcomes(store)) == 3 and len(calls) == 3


async def test_without_a_start_the_budget_and_dashboard_read_recent_claims(monkeypatch):
    store = MemoryStore()
    now = T0 + 100 * DAY_MS
    monkeypatch.setattr(quality, "_now_ms", lambda: now)
    monkeypatch.setitem(quality.EVENT_LANE, "BUDGET_FROM_MS", 0)
    await _judged(store, "hk", "ck", [("rejected", "unsupported")] * 50, at=now - 40 * DAY_MS)
    await _judged(store, "hk", "ck", [("provisional", None)] * 3, at=now - DAY_MS)
    assert (await quality.standing(store))["ck"]["junk"] == 0
    assert (await quality.multipliers(store))["ck"] == 1.0
    # A set start is read from, and a read from a given start is not cut.
    monkeypatch.setitem(quality.EVENT_LANE, "BUDGET_FROM_MS", now - 50 * DAY_MS)
    quality.reset()
    assert (await quality.standing(store))["ck"]["junk"] == 50
    assert len(await quality.outcomes(store, 0)) == 53


async def test_a_signed_epoch_is_answered_at_once_from_the_subnet_start(monkeypatch):
    """Decisions are stamped under the ledger lock, so a signed epoch's cut is final when
    signed: there is no wait for late writes."""
    from datetime import datetime, timezone
    start = T0 - DAY_MS
    monkeypatch.setattr(qm, "QUALITY_FROM", datetime.fromtimestamp(start / 1000, timezone.utc))
    store = MemoryStore()
    app = FastAPI()
    app.include_router(ledger_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    app.dependency_overrides[ledger_router.get_validator_hotkey] = lambda: "vali"
    await _judged(store, "hk", "ck", [("provisional", None)])
    await store.put_attestation(ledger_router.ledger.LEDGER_SIGNER, 7, {"capacity": 1.0},
                                issued_ms=T0 + 1)
    assert not hasattr(ledger_router, "_sleep") and not hasattr(ledger_router.ledger, "_sleep")
    body = TestClient(app).get("/ledger/quality", params={"epoch": 7, "from_ms": start}).json()
    assert (body["from_ms"], body["until_ms"]) == (start, T0 + 1)
    assert [r[:2] for r in body["outcomes"]] == [[0, 1]]


async def test_the_outcomes_cache_is_capped(monkeypatch):
    store = MemoryStore()
    await _judged(store, "hk", "ck", [("provisional", None)])
    for until in range(quality.CACHE_MAX + 10):
        await quality.outcomes(store, T0, T0 + until)
    assert len(quality._cache) == quality.CACHE_MAX
    assert (T0, T0 + quality.CACHE_MAX + 9) in quality._cache and (T0, T0) not in quality._cache


def test_the_dashboard_reports_the_start_it_scored_from(monkeypatch):
    from app.utils.clock import HOUR_MS
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: MemoryStore()
    client = TestClient(app)
    now = T0 + 100 * DAY_MS + 123
    monkeypatch.setattr(quality, "_now_ms", lambda: now)
    monkeypatch.setitem(quality.EVENT_LANE, "BUDGET_FROM_MS", 0)
    cut = now - quality.LOOKBACK_MS
    assert client.get("/dashboard/quality").json()["from_ms"] == cut - cut % HOUR_MS
    monkeypatch.setitem(quality.EVENT_LANE, "BUDGET_FROM_MS", T0)
    assert client.get("/dashboard/quality").json()["from_ms"] == T0
