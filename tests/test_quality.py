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
    assert body["owners"] == ["ck"] and body["outcomes"] == [[0, 1], [0, 0]]
    body = client.get("/ledger/quality", params={"from_ms": T0 + 1}).json()
    assert body["from_ms"] == T0 + 1 and body["outcomes"] == [[0, 0]]
    # An epoch's outcomes stop where its points were signed, whenever they are asked for.
    assert client.get("/ledger/quality", params={"epoch": 7}).status_code == 409
    await store.put_attestation(ledger_router.ledger.LEDGER_SIGNER, 7, {"capacity": 1.0})
    store.attestation_ms[(ledger_router.ledger.LEDGER_SIGNER, 7)] = T0 + 1
    await _judged(store, "hk", "ck", [("provisional", None)])
    body = client.get("/ledger/quality", params={"epoch": 7}).json()
    assert body["until_ms"] == T0 + 1 and body["outcomes"] == [[0, 1], [0, 1]]


async def test_the_dashboard_shows_each_coldkeys_standing():
    store = MemoryStore()
    await _judged(store, "spam", "ck-spam", [("rejected", "unsupported")] * 60)
    await _judged(store, "good", "ck-good", [("provisional", None)] * 3)
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    client = TestClient(app)
    body = client.get("/dashboard/quality").json()
    assert body["rule"]["half_life"] == 50 and body["rule"]["full"] == 0.25
    got = {r["coldkey"]: r for r in body["coldkeys"]}
    assert got["ck-spam"]["multiplier"] == 0 and got["ck-spam"]["junk"] == 60
    assert got["ck-good"]["multiplier"] == 1 and got["ck-good"]["paid"] == 3
    new = client.get("/dashboard/quality", params={"coldkey": "ck-new"}).json()["coldkeys"]
    assert new == [{"coldkey": "ck-new", "q": 0.3, "multiplier": 1.0, "paid": 0, "junk": 0}]


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
