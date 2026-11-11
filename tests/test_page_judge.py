"""The page-judge queue: claims wait for the listed validators' signed verdicts, which combine
into one stored record; the per-hotkey allowance; the routes and the liveness alarm."""

import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpharidge_ai.judge.client import ScriptedJudge
from alpharidge_ai.schema import Claim
from alpharidge_ai.validator.judge import verdict_body
from alpharidge_ai.verify.fetch import FetchedPage
from app import config
from app.config import EVENT_LANE
from app.routers import claims as claims_router, judge as judge_router
from app.services import judge, ops, verifier
from app.services.store import MemoryStore
from tests.test_verifier import RECEIVED_MS, base, make, run, submit  # noqa: F401,F811

QUOTE = "Acme Inc. agreed on Tuesday to acquire Beta Systems in a deal valued at $1.2 billion."
NOW = RECEIVED_MS + 1000


def claim(**kw):
    out = dict(kind="event", headline="Say yes", event_type="merger_acquisition",
               category="finance", event_date="2026-10-13", source_url="https://a.example/x",
               actors=[{"id_scheme": "other", "id": "a", "role": "acquirer", "name": "Acme"}],
               facts=[{"field": "deal_value", "value": 1.2, "unit": "usd_billions"}], quote=QUOTE)
    out.update(kw)
    return Claim(**out)


def page(h="h1"):
    return FetchedPage(url="https://a.example/x", final_url="https://a.example/x", status=200,
                       text=QUOTE, content_hash=h)


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"v1", "v2"})


async def post(store, item, validator, judge_=None, fixed=True, at=NOW):
    c = Claim.model_validate((await store.get_claim(item.claim_id)).payload)
    answers = (judge_ or ScriptedJudge()).judge_page(c, QUOTE) if fixed else {}
    body = verdict_body({"key": item.key, **item.task}, c, "theirs", answers, fixed,
                        None if fixed else "unsupported")
    return await judge.add_post(store, validator, body, 1, f"n-{validator}", "00", at)


def test_cache_key_covers_the_claim_its_figures_and_quote():
    key = judge.cache_key(1, claim(), "h1")
    assert judge.cache_key(2, claim(), "h1") != key
    assert judge.cache_key(1, claim(facts=[{"field": "deal_value", "value": 9.9,
                                             "unit": "usd_billions"}]), "h1") != key
    assert judge.cache_key(1, claim(quote="Acme Inc. agreed to buy Beta Systems."), "h1") != key
    assert judge.cache_key(1, claim(headline="Other"), "h1") == key


@pytest.mark.usefixtures("listed")
async def test_a_claim_waits_for_every_listed_validator_and_takes_the_mean(base):
    store, v = MemoryStore(), make()
    v.panel = None
    row = await run(v, store, await submit(store, f"{base}/supported"))
    assert row.status == "novelty" and row.id in v._judge_wait
    (item,) = await store.judge_items()
    assert (item.kind, item.claim_id, item.hotkey, item.task["url"]) == (
        "claim", row.id, "5Fminer", row.source_url)
    assert item.task["final_url"] and item.task["final_url"] == store.fetches[-1].final_url
    assert item.task["at"].startswith("2026-10-13T12:40")

    assert await post(store, item, "v1", ScriptedJudge(yes=0.9)) == "accepted"
    assert await post(store, item, "v1") == "duplicate"
    v._judge_wait.clear()
    assert (await v.verify(store, row, NOW)).status == "novelty"
    await post(store, item, "v2", ScriptedJudge(yes=0.7))
    v._judge_wait.clear()
    row = await v.verify(store, row, NOW)
    assert row.status == "fetched"
    rec = store.fetches[-1].check["judge"]
    assert rec["outcome"] == "pass" and rec["scores"]["event"] == pytest.approx(0.8)
    assert [p["validator"] for p in rec["posts"]] == ["v1", "v2"]
    assert json.loads(rec["posts"][0]["body"])["content_hash"] == "theirs"
    assert (await store.judge_items("done"))[0].id == item.id
    await v.fetcher.aclose()


@pytest.mark.usefixtures("listed")
async def test_one_post_is_enough_after_the_quorum_wait(base):
    store, v = MemoryStore(), make()
    v.panel = None
    row = await run(v, store, await submit(store, f"{base}/supported"))
    (item,) = await store.judge_items()
    await post(store, item, "v2")
    v._judge_wait.clear()
    assert (await v.verify(store, row, NOW)).status == "novelty"
    v._judge_wait.clear()
    later = item.created_ms + judge.JUDGE_QUORUM_WAIT_S * 1000
    row = await v.verify(store, row, later)
    rec = store.fetches[-1].check["judge"]
    assert row.status == "fetched" and [p["validator"] for p in rec["posts"]] == ["v2"]
    await v.fetcher.aclose()


@pytest.mark.usefixtures("listed")
async def test_a_failed_fixed_check_on_any_validators_fetch_rejects(base):
    store, v = MemoryStore(), make()
    v.panel = None
    row = await run(v, store, await submit(store, f"{base}/supported"))
    (item,) = await store.judge_items()
    await post(store, item, "v1")
    await post(store, item, "v2", fixed=False)
    v._judge_wait.clear()
    row = await v.verify(store, row, NOW)
    assert (row.status, row.reason) == ("rejected", "fixed_check")
    await v.fetcher.aclose()


async def test_the_allowance_counts_judge_items_per_hotkey_per_day(monkeypatch):
    store = MemoryStore()
    monkeypatch.setitem(EVENT_LANE, "JUDGE_NEW_HOTKEY_ITEMS", 1)
    task = {"kind": "claim", "url": "https://a.example/x"}
    ask = dict(claim_id=1, hotkey="new", has_passes=False, now_ms=NOW, task=task)
    assert await judge.page_verdict(store, claim(), page(), **ask) is None
    assert await judge.page_verdict(store, claim(), page(), **ask) is None
    over = await judge.page_verdict(store, claim(), page("h2"), **ask)
    assert over == {"outcome": judge.UNCLEAR, "reason": "judge_allowance"}
    # A scoop's found page does not count; a new day starts again.
    assert await judge.page_verdict(store, claim(), page("h3"), **{**ask, "claim_id": 2},
                                    by_hotkey=False) is None
    tomorrow = {**ask, "now_ms": NOW + 86_400_000}
    assert await judge.page_verdict(store, claim(), page("h4"), **tomorrow) is None
    assert [i.hotkey for i in await store.judge_items()] == ["new", None, "new"]
    assert (EVENT_LANE["JUDGE_HOTKEY_ITEMS"] // 10, EVENT_LANE["JUDGE_NEW_HOTKEY_ITEMS"]) == (
        266, 1)


@pytest.fixture
def judge_api(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"v1", "v2"})
    app = FastAPI()
    app.include_router(judge_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    client = TestClient(app)
    client.store = store
    return client


def auth(hotkey):
    return {"X-Auth-SS58Address": hotkey, "X-Auth-Signature": "00", "X-Auth-Message": "m",
            "X-Auth-Timestamp": "1700000000", "X-Auth-Nonce": "n1"}


async def test_listed_validators_pull_items_and_post_signed_verdicts(base, judge_api):
    store, v = judge_api.store, make()
    v.panel = None
    row = await run(v, store, await submit(store, f"{base}/supported"))
    assert judge_api.get("/judge/pending", headers=auth("v9")).status_code == 403
    got = judge_api.get("/judge/pending", headers=auth("v1")).json()
    (item,) = got["items"]
    assert got["types"] == list(EVENT_LANE["TYPES"])
    assert {"key", "claim_id", "body", "signature", "url", "at"} <= set(item)
    assert "source_id" not in item and "trust" not in item

    body = verdict_body(item, claim(), "h", {}, False, "unsupported")
    assert judge_api.post("/judge/verdicts?x=1", content=body,
                          headers=auth("v1")).status_code == 400
    r = judge_api.post("/judge/verdicts", content=body, headers=auth("v1"))
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    assert judge_api.post("/judge/verdicts", content=body, headers=auth("v1")).json() == {
        "status": "duplicate"}
    (saved,) = store.judge_post_rows
    assert (saved.body, saved.signed_at, saved.nonce) == (body.decode(), 1700000000, "n1")
    assert judge_api.get("/judge/pending", headers=auth("v1")).json()["items"] == []
    assert len(judge_api.get("/judge/pending", headers=auth("v2")).json()["items"]) == 1
    for bad, code in ((b"{", 400), (body.replace(b'"claim_id":"', b'"claim_id":"9'), 400),
                      (body.replace(item["key"].encode(), b"0" * 64), 404)):
        assert judge_api.post("/judge/verdicts", content=bad,
                              headers=auth("v2")).status_code == code

    # A claim that moved on is closed and no longer served.
    await store.update_claim(row.id, "novelty", status="rejected")
    assert judge_api.get("/judge/pending", headers=auth("v2")).json()["items"] == []
    assert judge_api.post("/judge/verdicts", content=body,
                          headers=auth("v2")).status_code == 409
    await v.fetcher.aclose()


async def test_judge_failing_alarms_when_no_validator_posts_in_twice_the_wait(base):
    store, v = MemoryStore(), make()
    v.panel = None
    await run(v, store, await submit(store, f"{base}/supported"))
    (item,) = await store.judge_items()
    wait = judge.JUDGE_QUORUM_WAIT_S * 1000
    assert await judge.failing(store, item.created_ms + 2 * wait - 1) == []
    (alarm,) = await judge.failing(store, item.created_ms + 2 * wait + 1)
    assert (alarm["kind"], alarm["waiting"]) == ("judge_failing", 1)
    got = await ops.alarms(store, item.created_ms + 2 * wait + 1, None)
    assert got["counts"].get("judge_failing") == 1
    await post(store, item, "v1")
    assert await judge.failing(store, item.created_ms + 2 * wait + 1) == []
    await v.fetcher.aclose()


async def test_an_item_whose_claim_was_rejected_after_both_posts_is_closed(base):
    store, v = MemoryStore(), make()
    v.panel = None
    row = await run(v, store, await submit(store, f"{base}/supported"))
    (item,) = await store.judge_items()
    await post(store, item, "v1")
    await post(store, item, "v2")
    assert await judge.close_moved(store) == 0
    await store.update_claim(row.id, "novelty", status="rejected", reason="contrastive_known")
    assert await judge.close_moved(store) == 1
    assert await store.judge_items() == []
    await v.fetcher.aclose()


async def test_an_item_no_validator_posts_on_waits_a_day_then_costs_the_miner_nothing(base):
    store, v = MemoryStore(), make()
    v.panel = None
    row = await run(v, store, await submit(store, f"{base}/supported"))
    (item,) = await store.judge_items()
    late = item.created_ms + judge.JUDGE_NO_POST_REJECT_S * 1000
    assert judge.JUDGE_NO_POST_REJECT_S == 24 * 3600
    assert await judge.failing(store, late - 1)
    v._judge_wait.clear()
    assert (await v.verify(store, row, late - 1)).status == "novelty"
    assert v._judge_wait[row.id] == late - 1 + verifier.JUDGE_IDLE_WAIT_S * 1000
    assert (await store.budget_use(row.hotkey, 0))[0] == 1
    v._judge_wait.clear()
    row = await v.verify(store, row, late)
    assert (row.status, row.reason) == ("rejected", "judge_unavailable")
    assert await store.judge_items() == [] and await judge.failing(store, late) == []
    assert (await store.budget_use(row.hotkey, 0))[0] == 0
    other = replace(row, id=row.id + 1)
    assert row.doc_hash and await v.repeats(store, other, row.doc_hash, late) == 0
    await store.update_claim(row.id, "rejected", reason="judge_reject")
    assert await v.repeats(store, other, row.doc_hash, late) == 1
    await v.fetcher.aclose()


@pytest.mark.usefixtures("listed")
async def test_the_last_listed_post_brings_the_claim_back_on_the_next_pass(base, monkeypatch):
    store, v = MemoryStore(), make()
    v.panel = None
    monkeypatch.setattr(verifier, "_default", v)
    row = await run(v, store, await submit(store, f"{base}/supported"))
    (item,) = await store.judge_items()
    assert row.id in v._judge_wait
    await post(store, item, "v1")
    assert row.id in v._judge_wait
    await post(store, item, "v2")
    assert row.id not in v._judge_wait
