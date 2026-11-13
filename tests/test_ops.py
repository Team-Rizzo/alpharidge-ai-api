"""Operator views and review flags: page flags, trust panel, alarms and held-claim decisions."""


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.config import EVENT_LANE
from app.jobs import trust_nightly
from app.routers import claims as claims_router
from app.routers import ops as ops_router
from app.routers import product as product_router
from app.services import ops, page_flags, trust
from app.services.store import (
    ClaimRow, LedgerRow, MemoryStore, ReverifyRow, SourceFetchRow, SourceRow,
)
from app.utils import block, hotkey_whitelist

from .pipeline import HOUR_MS, T0, Lane

DAY_MS = 24 * HOUR_MS
OPERATOR = {"X-Auth-SS58Address": "op1", "X-Auth-Signature": "00", "X-Auth-Message": "m",
            "X-Auth-Timestamp": "1"}


def _fetch(i: int, url: str, html: str = "", published=True, reason=None) -> SourceFetchRow:
    return SourceFetchRow(
        claim_id=i, verdict="pending", content_hash=None if reason else f"h{i}",
        final_url=url, published_ms=T0 if published else None, reason=reason,
        check={"page": page_flags.signals(html, url)} if html else None, fetched_ms=T0)


def _byline(name: str, extra: str = "") -> str:
    return (f'<script type="application/ld+json">{{"@type": "NewsArticle", "author": '
            f'{{"@type": "Person", "name": "{name}"{extra}}}}}</script>')


def test_page_signals_and_section():
    s = page_flags.signals(_byline("Ann Lee", ', "jobTitle": "Contributor"'),
                           "https://news.example.com/a")
    assert len(s["bylines"]) == 1 and s["contributor"] is True
    assert page_flags.signals(_byline("Ann Lee"), "https://x.com/a")["contributor"] is False
    assert page_flags.section("https://www.forbes.com/sites/ann/2026/10/13/x/") == \
        "forbes.com/sites/*"
    assert page_flags.section("https://news.example.com/2026/10/13/x") == "example.com/*"


async def test_nightly_flags_bylines_contributors_and_undated_once():
    store = MemoryStore()
    await store.add_source(SourceRow(domain="example.com", status="active", base_trust=0.8))
    rows = [_fetch(i, f"https://example.com/blogs/p{i}", _byline(f"Writer {i}"))
            for i in range(12)]
    rows += [_fetch(100 + i, f"https://example.com/voices/p{i}",
                    _byline("Staff", ', "url": "https://example.com/contributors/x"'))
             for i in range(3)]
    rows += [_fetch(200 + i, f"https://example.com/tag/p{i}", published=False) for i in range(6)]
    rows += [_fetch(300 + i, f"https://example.com/news/p{i}", _byline("Staff"))
             for i in range(20)]
    rows += [_fetch(400, "https://example.com/gone", reason="fetch_failed", published=False)]
    for r in rows:
        await store.add_source_fetch(r)

    assert await page_flags.run(store, T0 + HOUR_MS) == 3
    flags = await store.trust_log(actor="detector")
    assert sorted((f.field, f.new) for f in flags) == [
        ("flag:bylines", "example.com/blogs/*"), ("flag:contributor", "example.com/voices/*"),
        ("flag:undated", "example.com/tag/*")]
    assert all(f.source_id is not None for f in flags)
    await trust_nightly.run(store, T0 + 2 * HOUR_MS)
    assert len(await store.trust_log(actor="detector")) == 3


async def test_trust_panel_lists_band_changes():
    store = MemoryStore()
    old = await store.add_source(SourceRow(domain="old.example", status="active"))
    new = await store.add_source(SourceRow(domain="new.example", status="active"))
    now = T0 + 40 * DAY_MS
    await trust.log(store, "band:financial", "reported", "prize-eligible", "nightly", "nightly",
                    new.id, now_ms=now - DAY_MS)
    await trust.log(store, "status", "active", "banned", "spam", "operator:x", old.id,
                    now_ms=now - DAY_MS)
    panel = await ops.trust_panel(store, now)
    assert [(p["source"], p["new"]) for p in panel["promoted"]] == [("new.example",
                                                                     "prize-eligible")]
    assert [(p["source"], p["new"]) for p in panel["demoted"]] == [("old.example", "banned")]


@pytest.fixture
def ops_api(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(config, "OPERATOR_HOTKEYS", {"op1"})
    monkeypatch.setattr(ops_router, "_now_ms", lambda: T0 + HOUR_MS)
    monkeypatch.setattr(block, "current_block", _head)
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {"m1": "c1", "m2": "c1", "m3": "c2"})
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    client = TestClient(app)
    client.store = store
    return client


HEAD = 7_000_000


async def _head():
    return HEAD


def _claim(hotkey: str, **kw) -> ClaimRow:
    return ClaimRow(hotkey=hotkey, signature="00", nonce="n", signed_at=0, body_hash="h",
                    body="{}", received_ms=T0, bucket=T0 // 250, kind="event",
                    category="finance", event_type="merger_acquisition", payload={},
                    source_url="https://newsone.com/a", **kw)


async def test_alarms_are_operator_only_and_list_what_fires(ops_api, monkeypatch):
    # Two earning coldkeys: no share alarm, but c1's two hotkeys reach the hotkey limit.
    monkeypatch.setitem(config.PRIVATE, "ALARM_COLDKEY_HOTKEYS", 2)
    store = ops_api.store
    assert ops_api.get("/ops/alarms").status_code == 403
    epoch = HEAD // EVENT_LANE["BLOCK_LENGTH"]
    for hk, pts, cat in (("m1", 30.0, "finance"), ("m2", 30.0, "finance"),
                         ("m3", 40.0, "weather")):
        await store.append_ledger(LedgerRow(hk, None, None, "provisional", pts, epoch,
                                            category=cat))
    held = await store.create_claim(_claim("m3", status="provisional", held=True, route="event"))
    await store.put_reverify(ReverifyRow(held.id, "v1", epoch, "disagree"))
    for i in range(config.PRIVATE["ALARM_FETCH_MIN"]):
        await store.add_source_fetch(SourceFetchRow(
            claim_id=i, verdict="rejected", final_url="https://down.example/x",
            reason="fetch_failed", fetched_ms=T0 + HOUR_MS - 60_000))

    body = ops_api.get("/ops/alarms", headers=OPERATOR).json()
    assert body["counts"] == {"coldkey_concentration": 1, "fetch_failures": 1, "ledger_stale": 1, "reverify_disagree": 1,
                              "unknown_category": 1}
    assert [a["category"] for a in body["alarms"] if a["kind"] == "unknown_category"] == [
        "weather"]
    groups = {a["coldkey"]: a for a in body["alarms"] if a["kind"] == "coldkey_concentration"}
    assert groups["c1"]["hotkeys"] == ["m1", "m2"] and groups["c1"]["share"] == 0.6
    (rv,) = [a for a in body["alarms"] if a["kind"] == "reverify_disagree"]
    assert rv["claim_id"] == str(held.id) and rv["validators"] == ["v1"]

    bad = ops_api.post(f"/ops/claims/{held.id}/resolve", json={"action": "x", "reason": "r"},
                       headers=OPERATOR)
    assert bad.status_code == 422
    done = ops_api.post(f"/ops/claims/{held.id}/resolve",
                        json={"action": "clear", "reason": "checked by hand"}, headers=OPERATOR)
    assert done.json() == {"claim_id": str(held.id), "status": "provisional", "held": False}
    assert ops_api.get("/ops/trust", headers=OPERATOR).status_code == 200


async def test_blacklist_writes_are_operator_only(ops_api):
    store = ops_api.store
    assert ops_api.put("/ops/blacklist/m9", json={"reason": "r"}).status_code == 403
    assert ops_api.put("/ops/blacklist/m9", json={"reason": "r"}, headers=OPERATOR).status_code == 200
    assert await store.is_blacklisted("m9")
    assert ops_api.delete("/ops/blacklist/m9", headers=OPERATOR).status_code == 200
    assert not await store.is_blacklisted("m9")


async def test_stale_ledger_and_missed_reverify_alarm(monkeypatch):
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"v1", "v2"})
    store = MemoryStore()
    epoch = HEAD // EVENT_LANE["BLOCK_LENGTH"]
    old = epoch - 100
    await store.append_ledger(LedgerRow("m1", 5, None, "provisional", 1.0, old))
    await store.append_ledger(LedgerRow("m1", 6, None, "provisional", 1.0, epoch - 2))
    await store.add_reverify_report("v1", old, T0)
    await store.put_attestation("event-ledger", epoch - 5, {"perMinerPoints": {}})
    body = await ops.alarms(store, T0, HEAD)
    assert body["ledger"]["epochs_behind"] == 4
    missed = [(a["validator"], a["epoch"]) for a in body["alarms"]
              if a["kind"] == "reverify_missed"]
    assert missed == [("v2", old)]
    assert body["counts"]["ledger_stale"] == 1


async def test_operator_retraction_claws_without_a_penalty(ops_api):
    lane = Lane(check=True)
    await lane.sources()
    row = await lane.submit("m1", T0 + 60_000, facts=[
        {"field": "deal_value", "value": 1.2, "unit": "usd_billions"}])
    await lane.step(T0 + 61_000)
    ops_api.app.dependency_overrides[claims_router.get_store] = lambda: lane.store
    ops_api.app.dependency_overrides[product_router.get_entities] = lambda: lane.entities
    paid = lane.paid()
    body = {"kind": "correction", "slot": "stake_pct", "reason": "editor note"}
    url = f"/ops/claims/{row.id}/retract"
    assert ops_api.post(url, json=body).status_code == 403
    got = ops_api.post(url, json=body, headers=OPERATOR).json()
    assert got["applied"] is False and lane.store.claims[row.id].wrong is None

    got = ops_api.post(url, json={"reason": "story withdrawn"}, headers=OPERATOR).json()
    claim = lane.store.claims[row.id]
    assert got["applied"] is True and claim.trust_band == "prize-eligible"
    assert (claim.status, claim.wrong) == ("clawed_back", "retraction")
    assert lane.paid()["m1"] == pytest.approx(0.0)
    assert any(e.role == "retraction" for e in lane.store.source_event_rows)
    assert (await lane.store.get_event(claim.event_id)).marked == "retracted"
    (m,) = ops_api.get("/ops/wrong", headers=OPERATOR).json()["rows"]
    assert (m["hotkey"], m["band"], m["wrong"]) == ("m1", "prize-eligible", 1)
    assert m["earned"] == m["clawed"] == round(paid["m1"], 6) and m["penalty"] == 0


async def test_judge_audit_alarms_below_the_lower_bound(ops_api):
    from app.services.answer_key import accuracy
    from app.services.store import AccuracyCheckRow

    assert ops.wilson_lower(50, 50) == pytest.approx(0.9287, abs=1e-4)
    store = ops_api.store

    async def checks(n, wrong, start=0):
        for i in range(start, start + n):
            await store.add_accuracy_check(AccuracyCheckRow(
                claim_id=i, hotkey="m1", category="finance", kind=accuracy.ANSWER_KEY,
                correct=i >= start + wrong, at_ms=T0 + i, ref=str(i)))

    min_n = config.PRIVATE["AUDIT_MIN_N"]
    await checks(min_n - 1, 5)
    report = ops_api.get("/ops/audit", headers=OPERATOR).json()
    assert (report["checks"], report["alarm"]) == (min_n - 1, False)
    assert [d["claim_id"] for d in report["disagree"]] == ["0", "1", "2", "3", "4"]
    await checks(1, 0, start=min_n - 1)
    body = ops_api.get("/ops/alarms", headers=OPERATOR).json()
    (a,) = [a for a in body["alarms"] if a["kind"] == "judge_accuracy"]
    assert a["checks"] == min_n and a["lower_bound"] < config.PRIVATE["AUDIT_ALARM_LB"]


@pytest.mark.usefixtures("scoops_on")
async def test_conflicting_transfer_claims_are_listed_by_coldkey_or_gap(monkeypatch):
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {"m1": "c1", "m2": "c1", "m3": "c2"})
    lane = Lane(check=True)

    def move(to, **kw):
        return dict(event_type="transfer", category="sports", headline=f"Player X to {to}",
                    actors=[{"id_scheme": "wikidata", "id": f"Q{to[-1]}", "role": "club",
                             "name": to}],
                    facts=[{"field": "player", "value": "Player X"},
                           {"field": "to_team", "value": to}],
                    source_url=f"https://lowblog.net/{to[-1]}", **kw)

    a = await lane.submit("m1", T0, **move("Club A"))
    b = await lane.submit("m2", T0 + 30 * 60_000, **move("Club B"))
    await lane.submit("m3", T0 + 10 * 60_000, **move("Club A"))
    await lane.submit("m3", T0 + 31 * 60_000, **move("Club C"))
    body = await ops.hedge_pairs(lane.store, T0 - 7 * 86_400_000, lane.entities)
    got = [[(s["hotkey"], s["value"]) for s in p["claims"]] for p in body["pairs"]]
    assert got == [[("m1", ["Club A"]), ("m2", ["Club B"])],
                   [("m2", ["Club B"]), ("m3", ["Club C"])],
                   [("m3", ["Club A"]), ("m3", ["Club C"])]]
    assert [(p["same_coldkey"], p["apart_s"]) for p in body["pairs"]] == [
        (True, 1800), (False, 60), (True, 1260)]
    assert [s["claim_id"] for s in body["pairs"][0]["claims"]] == [str(a.id), str(b.id)]


async def test_a_coldkey_with_many_claws_alarms(monkeypatch):
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {"m1": "c1", "m2": "c1", "m3": "c2"})
    store = MemoryStore()
    now = T0 + 24 * HOUR_MS
    statuses = {"m1": ["clawed_back", "settled"], "m2": ["clawed_back", "provisional"],
                "m3": ["clawed_back", "settled", "settled", "settled"]}
    for hk, rows in statuses.items():
        for i, status in enumerate(rows):
            await store.create_claim(ClaimRow(
                hotkey=hk, signature="s", nonce=f"{hk}{i}", signed_at=None,
                body_hash=f"{hk}{i}", body="{}", received_ms=T0, bucket=0, kind="event",
                category="finance", event_type="earnings", payload={}, source_url="u",
                status=status, verified_ms=T0, trust_band="prize-eligible"))
    (alarm,) = await ops._claws(store, now)
    assert (alarm["coldkey"], alarm["paid"], alarm["clawed"]) == ("c1", 4, 2)
    assert await ops._claws(store, now + 6 * 24 * HOUR_MS) == []


async def test_refile_waits_its_turn_on_the_filing_host(ops_api, monkeypatch):
    from alpharidge_ai.verify.fetch import Paced
    from app.services import resolve

    row = await ops_api.store.create_claim(_claim("m1", status="verified"))

    async def paced(*a, **k):
        raise Paced(2.4)
    monkeypatch.setattr(resolve.Resolver, "refile", paced)
    r = ops_api.post(f"/ops/claims/{row.id}/refile", json={"reason": "r"}, headers=OPERATOR)
    assert r.status_code == 503 and r.headers["Retry-After"] == "3"


async def test_one_host_and_type_paid_too_often_raises_an_alarm(monkeypatch):
    store = MemoryStore()
    monkeypatch.setitem(EVENT_LANE, "ALARM_SOURCE_HOUR", 2)
    monkeypatch.setitem(EVENT_LANE, "ALARM_SOURCE_DAY", 3)
    monkeypatch.setitem(EVENT_LANE, "ALARM_SOURCE_HOSTS", "wire.example.com=10/40, bad")
    now = T0 + 2 * HOUR_MS
    for i in range(3):
        for host in ("journal.example.org", "wire.example.com"):
            row = await store.create_claim(_claim(f"hk{i}{host}"))
            await store.update_claim(row.id, "received", status="provisional", route="event",
                                     verified_ms=now - 60_000, source_url=f"https://{host}/{i}")
    got = await ops._sources(store, now)
    assert [(a["host"], a["paid_hour"], a["paid_day"]) for a in got] == [
        ("journal.example.org", 3, 3)]
    assert got[0]["limits"] == {"hour": 2, "day": 3}


def test_the_database_url_carries_the_process_connection_limit():
    from app.db import pooled_url
    url = "postgresql://u:p@db:5432/ar?schema=public"
    assert pooled_url(url) is None
    got = pooled_url(url, "10", "20")
    assert got == "postgresql://u:p@db:5432/ar?schema=public&connection_limit=10&pool_timeout=20"
    assert pooled_url(url + "&connection_limit=99", "10") == (
        "postgresql://u:p@db:5432/ar?schema=public&connection_limit=10")
