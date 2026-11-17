import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import claims as claims_router, dashboard as dashboard_router, ledger as ledger_router
from app.services import budget, quality
from app.services.store import MemoryStore
from app.utils import hotkey_whitelist
from tests.test_store_contract import T0, claim


async def _judged(store, hk, ck, outcomes):
    for i, (status, reason) in enumerate(outcomes):
        row = await store.create_claim(claim(hk, coldkey=ck))
        await store.update_claim(row.id, "received", status=status, reason=reason,
                                 judged_ms=T0 + i)


@pytest.fixture(autouse=True)
def _fresh():
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
    body = client.get("/ledger/quality", params={"from_ms": T0 + 1}).json()
    assert body["from_ms"] == T0 + 1 and body["outcomes"] == [[0, 0, ids[1]]]
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
