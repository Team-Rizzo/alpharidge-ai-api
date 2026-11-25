"""Product and dashboard endpoints over the pipeline harness: hold, confidence, feed."""

import hashlib
import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpharidge_ai.judge.client import ScriptedJudge
from alpharidge_ai.judge.prompt import verdict_of
from alpharidge_ai.schema import Claim
from app import config
from app.config import EVENT_LANE
from app.routers import claims as claims_router
from app.routers import dashboard as dashboard_router
from app.routers import product as product_router
from app.services import judge, ledger, product
from app.services.entities import EntityIndex, EntityRecord
from app.services.store import JudgeItemRow, JudgePostRow, LedgerRow, ReverifyRow, ScoopCandidateRow
from tests.pipeline import HEAD, T0, Lane

KEY = {"X-API-Key": "test-key"}
OWNER = {"X-Auth-SS58Address": "m1", "X-Auth-Signature": "00", "X-Auth-Message": "m",
         "X-Auth-Timestamp": "1"}
SEEN = T0 + 61_000
HOLD = int(EVENT_LANE["PUBLISH_HOLD_S"] * 1000)


def _lane(**kw) -> Lane:
    return Lane(check=True, entities=EntityIndex([
        EntityRecord("wikidata", "Q1", "Acme Inc.", kind="company"),
        EntityRecord("wikidata", "Q2", "Beta Systems", kind="company")]), **kw)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(config, "PRODUCT_API_KEYS", {hashlib.sha256(b"test-key").hexdigest()})
    clock = {"now": SEEN + HOLD + 1}
    monkeypatch.setattr(product_router, "_now_ms", lambda: clock["now"])
    monkeypatch.setattr(dashboard_router, "_now_ms", lambda: clock["now"])

    def make(lane: Lane) -> TestClient:
        app = FastAPI()
        app.include_router(product_router.router)
        app.include_router(dashboard_router.router)
        app.dependency_overrides[claims_router.get_store] = lambda: lane.store
        app.dependency_overrides[product_router.get_entities] = lambda: lane.entities
        client = TestClient(app)
        client.clock = clock
        return client
    return make


async def _event(lane: Lane, url="https://newsone.com/a", **kw) -> int:
    row = await lane.submit("m1", T0 + 60_000, source_url=url, **kw)
    await lane.step(SEEN)
    return lane.event_of(row)


async def test_publish_hold_and_api_key(api):
    lane = _lane()
    await lane.sources()
    eid = await _event(lane, facts=[{"field": "deal_value", "value": 1.2,
                                      "unit": "usd_billions"}])
    client = api(lane)
    assert client.get("/events").status_code == 401
    client.clock["now"] = SEEN + HOLD - 1
    assert client.get("/events", headers=KEY).json()["items"] == []
    assert client.get(f"/events/{eid}", headers=KEY).status_code == 404
    client.clock["now"] = SEEN + HOLD + 1
    (item,) = client.get("/events", headers=KEY).json()["items"]
    assert item["id"] == str(eid) and item["confidence"] == "reported"
    assert item["headline"] == "Acme Inc. and Beta Systems: acquisition announced"
    detail = client.get(f"/events/{eid}", headers=KEY).json()
    assert detail["facts"][0]["slot"] == "deal_value"
    assert detail["facts"][0]["first_seen_at"] == "2026-10-13T12:01:00.000Z"
    assert detail["official_match"] is False and detail["entities"][0]["id"] == "wikidata:Q1"
    assert "hotkey" not in json.dumps(detail) and "trust" not in json.dumps(detail)


async def test_detail_serves_enrichment(api):
    from app.services.store import EnrichRow

    lane = _lane()
    await lane.sources()
    eid = await _event(lane)
    client = api(lane)
    assert client.get(f"/events/{eid}", headers=KEY).json()["enrichment"] is None
    await lane.store.add_enrichments([eid], SEEN)
    (row,) = await lane.store.take_enrichments(1, 60_000, SEEN, False)
    await lane.store.finish_enrichment(EnrichRow(
        event_id=eid, status="done", attempts=row.attempts, summary="Acme bought Beta.",
        sentiment=None, country="US", region="Delaware"))
    assert client.get(f"/events/{eid}", headers=KEY).json()["enrichment"] == {
        "summary": "Acme bought Beta.", "sentiment": None, "country": "US",
        "region": "Delaware"}


@pytest.mark.usefixtures("scoops_on")
async def test_unverified_is_hidden_by_default(api, monkeypatch):
    lane = _lane()
    await lane.sources()
    eid = await _event(lane, url="https://lowblog.net/a")
    client = api(lane)
    assert (await lane.store.get_event(eid)).confidence == "unverified"
    assert client.get("/events", headers=KEY).json()["items"] == []
    shown = client.get("/events?include_unverified=true", headers=KEY).json()["items"]
    assert [i["id"] for i in shown] == [str(eid)]
    assert client.get("/feed", headers=KEY).json()["items"] == []
    monkeypatch.setitem(EVENT_LANE, "PRODUCT_KEYS_REQUIRED", False)
    assert client.get("/events?include_unverified=true").json()["items"] == []
    assert client.get(f"/events/{eid}?include_unverified=true").status_code == 404


async def test_person_claim_is_verified_and_served_unverified(api, monkeypatch):
    lane = _lane()
    await lane.sources()
    row = await lane.submit("m1", T0 + 60_000, source_url="https://newsone.com/a",
                            event_type="legal", actors=[
        {"id_scheme": "wikidata", "id": "Q1", "role": "plaintiff_company", "name": "Acme Inc."},
        {"id_scheme": "wikidata", "id": "Q99", "role": "defendant", "name": "Jane Doe"}])
    await lane.step(SEEN)
    assert lane.store.claims[row.id].status == "provisional"
    eid = lane.event_of(row)
    assert (await lane.store.get_event(eid)).confidence == "unverified"
    client = api(lane)
    assert client.get(f"/events/{eid}", headers=KEY).status_code == 404
    assert client.get("/events", headers=KEY).json()["items"] == []
    assert client.get("/feed", headers=KEY).json()["items"] == []
    shown = client.get("/events?include_unverified=true", headers=KEY).json()["items"]
    assert [i["id"] for i in shown] == [str(eid)]
    assert client.get(f"/events/{eid}?include_unverified=true", headers=KEY).status_code == 200
    (item,) = client.get("/dashboard/items").json()["items"]
    assert item["verification"] == "unverified" and "Jane Doe" in item["headline"]
    monkeypatch.setitem(EVENT_LANE, "API_UNVERIFIED", False)
    assert client.get("/events?include_unverified=true", headers=KEY).json()["items"] == []
    monkeypatch.setitem(EVENT_LANE, "PRODUCT_KEYS_REQUIRED", False)
    assert client.get("/events?include_unverified=true").json()["items"] == []


async def test_entity_timeline_spans_categories(api):
    lane = _lane()
    await lane.sources()
    deal = await _event(lane)
    halt = await _event(lane, url="https://wiretwo.org/h", event_type="trading_halt",
                        actors=[{"id_scheme": "wikidata", "id": "Q1", "name": "Acme Inc."}],
                        facts=[{"field": "exchange", "value": "Nasdaq"}])
    client = api(lane)
    body = client.get("/entities/wikidata:Q1", headers=KEY).json()
    assert body["entity"]["name"] == "Acme Inc."
    assert {(e["id"], e["category"]) for e in body["events"]} == {
        (str(deal), "finance"), (str(halt), "finance")}
    found = client.get("/events?entity=wikidata:Q1&type=trading_halt", headers=KEY).json()
    assert [e["id"] for e in found["items"]] == [str(halt)]
    assert client.get("/entities/wikidata:Q9", headers=KEY).status_code == 404


def test_people_need_a_public_role_record():
    role = ("office:Q11696",)
    entities = EntityIndex([
        EntityRecord("wikidata", "Q5", "Jane Roe", kind="official", xrefs=role),
        EntityRecord("wikidata", "Q6", "John Roe", kind="official"),
        EntityRecord("wikidata", "Q7", "Joe Doe", kind="person", xrefs=role),
        EntityRecord("wikidata", "Q8", "Acme Corp", kind="company")])
    assert entities.get("wikidata", "Q5").keys == ("wikidata:Q5",)

    def label(qid, name, event_type="appointment", role=None):
        claim = Claim(kind="event", headline="x", event_type=event_type, category="politics",
                      event_date="2026-10-13", source_url="https://gov.example/a",
                      actors=[{"id_scheme": "wikidata", "id": qid, "name": name,
                               "role": role}])
        return product.labelled_claim(claim, entities)

    assert not label("Q5", "Jane Roe")
    assert label("Q6", "John Roe") and label("Q7", "Joe Doe")
    assert label("Q5", "Joe Doe")
    assert label("Q8", "Joe Doe", role="person")
    assert label("Q5", "Jane Roe", event_type="legal")
    assert not label("Q9", "Jane Smith", event_type="merger_acquisition")
    assert label("Q9", "Jane Smith") and label("Q8", "Jane Smith")
    assert not label("Q8", "Acme Corp", event_type="legal")


async def test_dashboard_shows_route_and_check(api, monkeypatch):
    lane = _lane()
    client = api(lane)
    assert client.get("/dashboard/miners").json() == {
        "window": "24h", "category": None, "miners": [], "total": 0}
    await lane.sources()
    await _event(lane)
    client.clock["now"] = SEEN + HOLD - 1
    assert client.get("/dashboard/items").json()["items"] == []
    client.clock["now"] = SEEN + HOLD + 1
    (item,) = client.get("/dashboard/items").json()["items"]
    assert item["route"] == "event" and item["status"] == "provisional"
    assert item["verification"] == "reported"
    assert (item["source_domain"], item["trust_band"]) == (None, None)
    assert item["check"] is not None and item["receipt"] is None
    lane.store.receipt_rows.append({"seq": 1, "claim_id": item["id"], "hash": "h"})
    assert client.get("/dashboard/items").json()["items"][0]["receipt"]["seq"] == 1
    (event,) = client.get("/events", headers=KEY).json()["items"]
    assert event["source_url"] is None and product.OPEN_AT not in event
    monkeypatch.setattr(config, "OPERATOR_HOTKEYS", {"op1"})
    operator = {**OWNER, "X-Auth-SS58Address": "op1"}
    assert client.get("/dashboard/items", headers=operator).json()["items"][0]["source_domain"]
    (item,) = client.get("/dashboard/items", headers=OWNER).json()["items"]
    assert item["check"]["verdict"] == "supported" and item["check"]["judge"] == "pass"
    assert item["trust_band"] == "prize-eligible" and item["source_domain"] == "newsone.com"
    assert item["points"]["provisional"] > 0 and item["points"]["clawed"] == 0
    (miner,) = client.get("/dashboard/miners").json()["miners"]
    assert miner["hotkey"] == "m1" and miner["claims"] == 1 and miner["verified"] == 1
    assert miner["coldkey"] == "m1"
    assert miner["points"]["event"]["provisional"] == pytest.approx(item["points"]["provisional"])
    assert miner["budget"]["limit"] > 0
    assert miner["budget"]["scoops"] is True
    paid = miner["total_points"]
    lane.store.ledger.append(replace(lane.store.ledger[0], id=None, kind=ledger.PENALTY,
                                     points=-1.0))
    (miner,) = client.get("/dashboard/miners").json()["miners"]
    assert miner["points"]["event"]["penalty"] == 1.0
    assert miner["total_points"] == pytest.approx(paid - 1.0)
    lane.store.ledger.pop()
    # Debt carried into the signed window counts, also for a hotkey with no recent claims.
    epoch = lane.store.ledger[0].epoch
    await lane.store.put_attestation(ledger.LEDGER_SIGNER, epoch, {"totalPoints": 0.0})
    for hk in ("m1", "m9"):
        lane.store.ledger.append(LedgerRow(hk, None, None, ledger.CARRY, -2.0, epoch,
                                           route=ledger.CARRY))
    miners = {m["hotkey"]: m for m in client.get("/dashboard/miners").json()["miners"]}
    assert miners["m1"]["carried"] == 2.0
    assert miners["m1"]["total_points"] == pytest.approx(paid - 2.0)
    assert miners["m9"]["claims"] == 0 and miners["m9"]["total_points"] == -2.0
    del lane.store.ledger[-2:]
    # Everyone sees the source once the claim settles.
    monkeypatch.setitem(EVENT_LANE, "PRODUCT_KEYS_REQUIRED", False)
    assert client.get("/events").json()["items"][0]["source_url"] is None
    claim = next(iter(lane.store.claims.values()))
    claim.status, claim.reason = "rejected", "judge_reject"
    assert client.get("/dashboard/miners?window=7d").json()["miners"][0]["rejected"] == {
        "judge_reject": 1}
    assert client.get("/dashboard/items").json()["items"][0]["reason"] == "judge_reject"
    (item,) = client.get("/dashboard/items?type=merger_acquisition&reason=judge_reject").json()[
        "items"]
    assert (item["event_type"], item["stage"], item["event_date"]) == (
        "merger_acquisition", "announced", "2026-10-13")
    assert client.get("/dashboard/items?reason=known_event").json()["items"] == []
    assert client.get("/dashboard/items?type=earnings").json()["items"] == []
    assert client.get("/dashboard/items?type=nope").status_code == 400
    assert client.get("/dashboard/items?reason=Bad%20one").status_code == 400
    client.clock["now"] = SEEN + product.settle_ms() + 1
    claim.status, claim.reason = "provisional", None
    # A claim's source never reaches the public, even settled; API-key callers get it then.
    public = client.get("/dashboard/items").json()["items"][0]
    assert public["source_domain"] is None and public["trust_band"]
    assert client.get("/events").json()["items"][0]["source_url"] is None
    assert client.get("/events", headers=KEY).json()["items"][0]["source_url"] == \
        "https://newsone.com/a"


async def test_items_show_each_validator_only_once_open(api, monkeypatch):
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"v1", "v2"})
    lane = _lane()
    lane.panel.judge = {"v1": ScriptedJudge({"subject": "yes"}),
                        "v2": ScriptedJudge({"subject": "yes", "event": "no"})}
    await lane.sources()
    await _event(lane)
    claim = next(iter(lane.store.claims.values()))
    item = lane.panel.items[0]
    for p in await lane.store.judge_posts([item.id]):
        answers = json.loads(p.body)["answers"]
        parsed = Claim.model_validate_json(claim.body)
        assert judge.post_outcome(p.body)[0] == verdict_of(answers, parsed)["outcome"]
    await lane.store.put_reverify(ReverifyRow(claim.id, "v1", 1, "changed", {"x": 1}))
    await lane.store.put_reverify(ReverifyRow(claim.id, "v2", 1, "disagree"))
    claim.route = "scoop"
    for who, status in (("v2", "rejected"), ("v2", "confirmed"), ("api", "confirmed")):
        await lane.store.add_scoop_candidate(ScoopCandidateRow(
            claim.id, f"https://found.example/{who}{status}", who, SEEN, status=status,
            reason="low_trust" if status == "rejected" else None))
    client = api(lane)
    assert client.get("/dashboard/items").json()["items"][0]["validators"] is None
    want = [{"hotkey": "v1", "verdict": "pass", "reason": None, "recheck": "agree",
             "confirm": None},
            {"hotkey": "v2", "verdict": "reject", "reason": "judge_reject",
             "recheck": "disagree", "confirm": {"status": "confirmed", "reason": None}}]
    (row,) = client.get("/dashboard/items", headers=OWNER).json()["items"]
    assert row["validators"] == want and row["reverify"] == {"changed": 1, "disagree": 1}
    client.clock["now"] = claim.received_ms + product.settle_ms() + 1
    (row,) = client.get("/dashboard/items").json()["items"]
    assert row["validators"] == want
    text = json.dumps(row["validators"])
    assert "found.example" not in text and "probabilities" not in text


async def test_validator_summary_agreement(api, monkeypatch):
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"v1", "v2"})
    monkeypatch.setattr(dashboard_router.block, "current_block", _head)
    lane = _lane()
    client = api(lane)
    now = client.clock["now"]
    store = lane.store
    old = [await lane.submit("m1", T0 - 5 * 24 * 3600 * 1000) for _ in range(3)]
    new = await lane.submit("m1", now - 1000)
    ep = ledger.epoch_of(HEAD)
    seen = {"v1": [ep - 100, ep - 200], "v2": [ep - 100], "v3": [ep - 100]}
    for e in (ep - 100, ep - 200):
        store.ledger.append(LedgerRow("m1", old[0].id, None, ledger.PROVISIONAL, 1.0, e))
    for v, epochs in seen.items():
        for e in epochs:
            await store.add_reverify_report(v, e, now - 3600_000)

    def pass_or(passed):
        p = 0.9 if passed else 0.1
        return json.dumps({"answers": {"event": {"type": "choice", "choice": "yes",
                                                 "probabilities": {"yes": p}}},
                           "fixed_check": {"passed": True}})

    votes = [(old[0], {"v1": True, "v2": True, "v3": True}),
             (old[1], {"v1": True, "v2": False, "v3": False}),
             (old[2], {"v1": False}), (new, {"v1": True, "v2": False})]
    for n, (c, by) in enumerate(votes):
        item = await store.add_judge_item(JudgeItemRow(f"k{n}", c.id, "claim", {}, now))
        for v, passed in by.items():
            await store.add_judge_post(JudgePostRow(item.id, v, pass_or(passed), 0, "n", "s",
                                                    now - 1000))
    for c, v, verdict in ((old[0], "v1", "agree"), (old[0], "v2", "changed"),
                          (old[1], "v1", "disagree"), (old[1], "v2", "agree"),
                          (old[1], "v3", "unreachable")):
        await store.put_reverify(ReverifyRow(c.id, v, ep - 100, verdict))
    body = client.get("/dashboard/validators").json()
    assert body["window"] == "7d"
    got = {v["hotkey"]: v for v in body["validators"]}
    assert {k: (v["verdicts"], v["verdict_agreement"], v["rechecks"], v["recheck_agreement"],
                v["missed_epochs"], v["last_rechecked_epoch"]) for k, v in got.items()} == {
        "v1": (3, 0.5, 2, 0.5, 0, ep - 100), "v2": (2, 1.0, 2, 0.5, 1, ep - 100),
        "v3": (2, 1.0, 1, None, None, ep - 100)}
    assert got["v1"]["last_judged_epoch"] == ep and got["v1"]["last_weights_epoch"] is None
    assert client.get("/dashboard/validators?window=1y").status_code == 400


async def _head():
    return HEAD


async def test_sources_follow_the_lead_claims_own_settle_time(api):
    lane = _lane()
    await lane.sources()
    eid = await _event(lane, facts=[{"field": "deal_value", "value": 1.2,
                                      "unit": "usd_billions"}])
    client = api(lane)
    lead = next(iter(lane.store.claims.values()))
    client.clock["now"] = lead.settle_ms - 1
    (rec,) = client.get("/feed", headers=KEY).json()["items"]
    assert rec["source_url"] is None and product.OPEN_AT not in json.dumps(rec)
    client.clock["now"] = lead.settle_ms + 1
    (rec,) = client.get("/feed", headers=KEY).json()["items"]
    assert rec["source_url"] == rec["facts"][0]["source_url"] == "https://newsone.com/a"
    lead.settle_ms += 10 * product.settle_ms()
    detail = client.get(f"/events/{eid}", headers=KEY).json()
    assert detail["source_url"] is None and detail["facts"][0]["source_url"] is None
    client.clock["now"] = lead.settle_ms + 1
    detail = client.get(f"/events/{eid}", headers=KEY).json()
    assert detail["source_url"] == detail["facts"][0]["source_url"] == "https://newsone.com/a"


def test_mechanism_summary_is_public(api, monkeypatch):
    from app.services import ledger
    from app.utils import mechanism_profile

    async def profiles():
        return {"current": {"settlement": {"C": 1234.0},
                            "ledger": {"window_epochs": 216}}}
    monkeypatch.setattr(mechanism_profile, "load_profiles", profiles)
    monkeypatch.setattr(ledger, "_params", None)
    got = api(_lane()).get("/dashboard/mechanism").json()
    assert (got["capacity"], got["window_epochs"], got["window_hours"]) == (1234.0, 216, 72.0)
    assert got["epoch_seconds"] == 1200 and got["last_signed"] is None


async def test_api_keys_can_be_revoked():
    import importlib.util
    from pathlib import Path

    from app.services.store import MemoryStore

    spec = importlib.util.spec_from_file_location(
        "api_keys", Path(__file__).resolve().parent.parent / "scripts" / "api_keys.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    store = MemoryStore()
    a = await tool.run(store, tool.parse(["add", "acme"]))
    b = await tool.run(store, tool.parse(["add", "acme"]))
    c = await tool.run(store, tool.parse(["add", "beta"]))
    assert await tool.run(store, tool.parse(["revoke", f"--key={a}"])) == "revoked 1"
    assert await store.api_key_owner(tool._hash(a)) is None
    assert await store.api_key_owner(tool._hash(b)) == "acme"
    assert await tool.run(store, tool.parse(["revoke", "acme"])) == "revoked 1"
    assert await store.api_key_owner(tool._hash(c)) == "beta"
    with pytest.raises(SystemExit):
        tool.parse(["revoke"])


async def _story(store, lead_url="https://finance.yahoo.com/news/acme-beta"):
    import uuid
    from datetime import date

    from app.services.store import ClaimRow, EventRecord, SourceFetchRow, SourceRow
    from tests.pipeline import payload

    hosts = {"finance.yahoo.com": "active", "uk.finance.yahoo.com": "active",
             "newsone.com": "active", "prnewswire.com": "active", "wiretwo.org": "active",
             "dailythree.net": "active", "banned.com": "banned", "lowblog.net": "active"}
    src = {h: await store.add_source(SourceRow(domain=h, owner_id=f"domain:{h}", status=s))
           for h, s in hosts.items()}
    ev = await store.add_event(EventRecord("merger_acquisition", "finance", date(2026, 10, 13),
                                           "fp-" + uuid.uuid4().hex, publish_ms=T0))

    async def claim(url, published=None, settle=None, trust=0.9, status="provisional",
                    check=None):
        row = await store.create_claim(ClaimRow(
            hotkey="m1", signature="s", nonce=uuid.uuid4().hex, signed_at=T0 // 1000,
            body_hash=uuid.uuid4().hex, body="{}", received_ms=T0, bucket=0, kind="event",
            category="finance", event_type="merger_acquisition",
            payload=payload(source_url=url), source_url=url, status=status, route="event",
            event_id=ev.id, source_id=src[url.split("/")[2].removeprefix("www.")].id,
            trust=trust, settle_ms=settle))
        if published is not None or check is not None:
            await store.add_source_fetch(SourceFetchRow(claim_id=row.id, verdict="supported",
                                                        published_ms=published, check=check))
        return row

    lead = await claim(lead_url, T0 + 600_000, T0 + 3_600_000)
    await store.update_event(ev.id, first_claim_id=lead.id)
    return replace(ev, first_claim_id=lead.id), lead, claim, src


async def test_sources_name_the_original_publisher_once_per_page():
    from app.services.store import MemoryStore

    store = MemoryStore()
    ev, lead, claim, _ = await _story(store)
    await claim("https://www.newsone.com/a", T0, T0 + 7_200_000)
    await claim("https://www.newsone.com/a", None, T0 + 1_800_000)
    await claim("https://prnewswire.com/news-releases/acme-beta", T0 + 1_200_000,
                T0 + 3_600_000)
    await claim("https://banned.com/a", T0 - 60_000, T0)
    await claim("https://lowblog.net/a", T0 - 60_000, T0, trust=0.1)
    await claim("https://dailythree.net/a", T0 - 60_000, T0, status="rejected")
    got = (await product.sources_of(store, [ev]))[ev.id]
    assert [(s["publisher"], s["kind"], s["original"]) for s in got] == [
        ("newsone.com", "news", True), ("finance.yahoo.com", "aggregator", False),
        ("prnewswire.com", "press_release", False)]
    assert got[0]["published_at"] == "2026-10-13T12:00:00.000Z"
    assert got[0][product.OPEN_AT] == T0 + 1_800_000
    s = await product.summary(store, ev, EntityIndex())
    assert (s["publisher"], s["source_kind"]) == ("newsone.com", "news")
    assert s["source_url"] == lead.source_url
    d = await product.detail(store, ev, EntityIndex())
    assert d["sources"] == got and d["publisher"] == "newsone.com"
    out = json.dumps(product.hide_sources(d, T0 + 10 * 3_600_000))
    assert "_published_ms" not in out and "trust" not in out and "m1" not in out


async def test_an_undated_page_loses_to_a_dated_one_and_a_copy_to_the_original():
    from app.services.store import MemoryStore

    store = MemoryStore()
    ev, lead, claim, _ = await _story(store, "https://uk.finance.yahoo.com/news/acme")
    s = await product.summary(store, ev, EntityIndex())
    assert (s["publisher"], s["source_kind"]) == ("uk.finance.yahoo.com", "aggregator")
    await store.update_claim(lead.id, "provisional", trust=0.1)
    s = await product.summary(store, ev, EntityIndex())
    assert (s["publisher"], s["source_kind"]) == ("uk.finance.yahoo.com", "aggregator")
    store.fetches.clear()
    await store.update_claim(lead.id, "provisional", trust=0.9)
    await claim("https://wiretwo.org/a")
    got = (await product.sources_of(store, [ev]))[ev.id]
    assert [(s["publisher"], s["original"]) for s in got] == [
        ("wiretwo.org", True), ("uk.finance.yahoo.com", False)]
    await claim("https://dailythree.net/a", T0 + 3_600_000)
    got = (await product.sources_of(store, [ev]))[ev.id]
    assert [s["publisher"] for s in got] == ["dailythree.net", "wiretwo.org",
                                            "uk.finance.yahoo.com"]
    assert got[1]["published_at"] is None


async def test_source_links_open_when_the_claim_behind_them_settles():
    from app.services import trust
    from app.services.store import MemoryStore, SourceEventRow

    store = MemoryStore()
    ev, lead, claim, src = await _story(store)
    scoop = await claim("https://dailythree.net/tip", T0 - 600_000, T0 + 7_200_000,
                        check={"judge": {"outcome": "pass",
                                         "confirm": {"url": "https://wiretwo.org/confirm"}}})
    report = await claim("https://www.newsone.com/report", status="rejected", trust=0.1)
    gone = await claim("https://prnewswire.com/news-releases/acme", status="rejected")
    for source, role, row, published in (
            ("wiretwo.org", trust.CONFIRM, scoop, T0 + 60_000),
            ("newsone.com", trust.CONFIRM, report, T0 + 120_000),
            ("prnewswire.com", trust.FIRST, gone, T0 + 180_000),
            ("banned.com", trust.CONFIRM, report, T0)):
        await store.add_source_event(SourceEventRow(
            source_id=src[source].id, role=role, category="finance", at_ms=T0,
            event_id=ev.id, claim_id=row.id, published_ms=published))
    got = {s["publisher"]: s for s in (await product.sources_of(store, [ev]))[ev.id]}
    assert set(got) == {"dailythree.net", "wiretwo.org", "newsone.com", "prnewswire.com",
                        "finance.yahoo.com"}
    assert got["dailythree.net"]["original"]
    assert got["wiretwo.org"]["url"] == "https://wiretwo.org/confirm"
    assert got["wiretwo.org"][product.OPEN_AT] == scoop.settle_ms
    assert got["newsone.com"][product.OPEN_AT] == lead.settle_ms
    assert got["prnewswire.com"][product.OPEN_AT] is None

    def urls(now_ms):
        d = product.hide_sources({"sources": list(got.values())}, now_ms)
        return {s["publisher"]: s["url"] for s in d["sources"]}

    assert set(urls(None).values()) == {None}
    assert set(urls(lead.settle_ms - 1).values()) == {None}
    mid = urls(lead.settle_ms)
    assert mid["newsone.com"] == "https://www.newsone.com/report"
    assert mid["finance.yahoo.com"] == lead.source_url
    assert mid["wiretwo.org"] is None and mid["prnewswire.com"] is None
    late = urls(scoop.settle_ms)
    assert late["wiretwo.org"] == "https://wiretwo.org/confirm"
    assert late["prnewswire.com"] is None
    assert {s["kind"] for s in product.hide_sources(list(got.values()), None)} == {
        "news", "aggregator", "press_release"}


async def test_the_list_serves_each_events_publisher(api, monkeypatch):
    lane = _lane()
    await lane.sources()
    await _event(lane)
    await _event(lane, url="https://wiretwo.org/h", event_type="trading_halt",
                 actors=[{"id_scheme": "wikidata", "id": "Q1", "name": "Acme Inc."}],
                 facts=[{"field": "exchange", "value": "Nasdaq"}])
    client = api(lane)
    items = client.get("/events", headers=KEY).json()["items"]
    assert {(i["publisher"], i["source_kind"]) for i in items} == {
        ("newsone.com", "news"), ("wiretwo.org", "news")}
    feed = client.get("/feed", headers=KEY).json()["items"]
    assert {r["publisher"] for r in feed if r["kind"] == "event"} == {
        "newsone.com", "wiretwo.org"}
    detail = client.get(f"/events/{items[0]['id']}", headers=KEY).json()
    (source,) = detail["sources"]
    assert source["original"] and source["url"] is None
    assert product.OPEN_AT not in json.dumps(detail)
    client.clock["now"] = max(c.settle_ms for c in lane.store.claims.values()) + 1
    detail = client.get(f"/events/{items[0]['id']}", headers=KEY).json()
    assert detail["sources"][0]["url"] == detail["source_url"]
    monkeypatch.setitem(EVENT_LANE, "PRODUCT_KEYS_REQUIRED", False)
    public = client.get(f"/events/{items[0]['id']}").json()
    assert public["sources"][0]["url"] is None
    assert public["sources"][0]["publisher"] == detail["sources"][0]["publisher"]
