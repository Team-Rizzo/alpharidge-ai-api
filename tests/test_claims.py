"""Claim intake: signed push, receipt clock, line order, check budget, ledger epochs."""

import asyncio
import hashlib
import secrets
import time
from dataclasses import replace
from datetime import date

import pytest
from bittensor_wallet import Keypair
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpharidge_ai.schema import Claim, canonical_body
from alpharidge_ai.verify.fetch import Paced
from app.jobs import worker
from app.routers import claims as claims_router
from app.services import budget, intake, ledger, line
from app.services.store import EventRecord, LedgerRow, MemoryStore
from app.utils import auth, hotkey_whitelist
from app.utils.auth import AuthRequest
from app.utils.receipt import ReceiptClock

MINER = Keypair.create_from_seed("0x" + "a1" * 32)
RIVAL = Keypair.create_from_seed("0x" + "b2" * 32)
VALI = Keypair.create_from_seed("0x" + "c3" * 32)
DAY_MS = 24 * 3600 * 1000
COMPANY = {"id_scheme": "wikidata", "id": "Q1", "role": "company", "name": "Acme"}


def claim(**kw) -> Claim:
    base = dict(kind="event", headline="Acme agrees to buy Widget Co",
                event_type="merger_acquisition", category="sports",
                actors=[{"id_scheme": "wikidata", "id": "Q1", "role": "target", "name": "Acme"},
                        {"id_scheme": "wikidata", "id": "Q2", "role": "acquirer",
                         "name": "Widget Co"}],
                event_date="2026-10-13", source_url="https://example.org/a",
                quote="Widget Co agreed to buy Acme, the companies said.")
    base.update(kw)
    return Claim(**base)


def signed(kp, method, target, body=b"", nonce=None):
    ts, nonce = int(time.time()), nonce or secrets.token_hex(16)
    bh = hashlib.sha256(body).hexdigest()
    msg = auth.create_auth_message(method, target, bh, ts, nonce)
    return {"X-Auth-SS58Address": kp.ss58_address, "X-Auth-Signature": kp.sign(msg.encode()).hex(),
            "X-Auth-Message": msg, "X-Auth-Timestamp": str(ts), "X-Auth-Nonce": nonce,
            "X-Auth-Body-SHA256": bh, "Content-Type": "application/json"}


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setattr(auth.auth_config, "enabled", True)
    monkeypatch.setattr(hotkey_whitelist, "_miners", [MINER.ss58_address, RIVAL.ss58_address])
    monkeypatch.setattr(hotkey_whitelist, "_validators", [VALI.ss58_address])
    monkeypatch.setattr(hotkey_whitelist, "ALLOW_MANUAL_HOTKEYS", False)
    auth.nonce_cache.clear()
    app = FastAPI()
    app.add_middleware(ReceiptClock)
    app.include_router(claims_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: store
    return TestClient(app)


def post(client, kp, body, **kw):
    return client.post("/claims", content=body, headers=signed(kp, "POST", "/claims", body, **kw))


def test_signed_claim_is_stored_queued_with_receipt(client, store):
    before = time.time_ns() // 1_000_000
    r = post(client, MINER, canonical_body(claim()))
    assert r.status_code == 202
    out = r.json()
    row = store.claims[int(out["id"])]
    assert out["status"] == "queued" and out["received_at"].endswith("Z")
    assert before <= row.received_ms <= time.time_ns() // 1_000_000
    assert out["bucket"] == row.bucket == row.received_ms // 250
    assert row.category == "finance"
    assert [j.kind for j in store.jobs.values()] == ["fetch"]


def test_type_off_is_rejected_at_intake(client, store):
    body = canonical_body(claim(event_type="guidance", headline="Acme raises guidance",
                                actors=[COMPANY]))
    r = post(client, MINER, body)
    assert r.status_code == 202
    row = store.claims[int(r.json()["id"])]
    assert (row.status, row.reason) == ("rejected", "type_off")
    assert not store.jobs


def test_fact_and_stage_claims_are_rejected_at_intake(client, store):
    body = canonical_body(claim(kind="fact", event_id="1",
                                facts=[{"field": "deal_value", "value": 1.2,
                                        "unit": "usd_billions"}]))
    row = store.claims[int(post(client, MINER, body).json()["id"])]
    assert (row.status, row.reason) == ("rejected", "kind_off")
    assert not store.jobs


def test_an_event_claim_needs_a_quote(client, store):
    r = post(client, MINER, canonical_body(claim(quote=None)))
    assert (r.status_code, r.json()["detail"]) == (400, "missing_required") and not store.claims


def test_personnel_below_top_office_is_rejected_at_intake(client, store):
    def person(role):
        facts = [{"field": "person", "value": "Jane Doe"}] + (
            [{"field": "role", "value": role}] if role else [])
        return canonical_body(claim(event_type="personnel", headline="Acme names Jane Doe",
                                    facts=facts, actors=[COMPANY]))
    row = store.claims[int(post(client, MINER, person("director")).json()["id"])]
    assert (row.status, row.reason) == ("rejected", "role_off")
    assert not store.jobs
    r = post(client, MINER, person(None))
    assert (r.status_code, r.json()["detail"]) == (400, "missing_required")
    row = store.claims[int(post(client, MINER, person("cfo")).json()["id"])]
    assert row.status == "queued" and row.reason is None


def test_a_signed_claim_post_with_a_query_string_is_refused(client, store):
    body = canonical_body(claim())
    r = client.post("/claims?x=1", content=body,
                    headers=signed(MINER, "POST", "/claims?x=1", body))
    assert (r.status_code, r.json()["detail"]) == (400, "query_not_allowed")
    assert not store.claims


def test_signature_and_role_are_checked(client):
    body = canonical_body(claim())
    h = signed(MINER, "POST", "/claims", body)
    h["X-Auth-Signature"] = RIVAL.sign(h["X-Auth-Message"].encode()).hex()
    assert client.post("/claims", content=body, headers=h).status_code == 401
    assert post(client, VALI, body).status_code == 403
    r = post(client, MINER, body)
    path = f"/claims/{r.json()['id']}"
    assert client.get(path, headers=signed(MINER, "GET", path)).json()["status"] == "queued"
    assert client.get(path, headers=signed(VALI, "GET", path)).status_code == 200
    assert client.get(path, headers=signed(RIVAL, "GET", path)).status_code == 404


async def test_event_id_is_shown_only_once_published(client, store):
    r = post(client, MINER, canonical_body(claim()))
    cid = int(r.json()["id"])
    ev = await store.add_event(EventRecord("merger_acquisition", "finance", date(2026, 10, 13),
                                           "fp", confidence="line"))
    await store.update_claim(cid, "queued", event_id=ev.id, novelty="new")
    path = f"/claims/{cid}"
    got = client.get(path, headers=signed(MINER, "GET", path)).json()
    assert got["event_id"] is None and got["novelty"] is None
    await store.update_event(ev.id, confidence="reported", publish_ms=0)
    got = client.get(path, headers=signed(MINER, "GET", path)).json()
    assert got["event_id"] == str(ev.id) and got["novelty"] == "new"


async def test_claim_shows_the_check_detail_of_its_rejection(client, store):
    from app.services.store import SourceFetchRow
    cid = int(post(client, MINER, canonical_body(claim())).json()["id"])
    await store.add_source_fetch(SourceFetchRow(cid, "rejected", reason="unsupported",
                                                check={"detail": {"missing": ["fact:eps"]}}))
    path = f"/claims/{cid}"
    assert client.get(path, headers=signed(MINER, "GET", path)).json()["reason_detail"] is None
    await store.update_claim(cid, "queued", status="rejected", reason="unsupported")
    got = client.get(path, headers=signed(MINER, "GET", path)).json()
    assert got["reason_detail"] == {"missing": ["fact:eps"]}


async def test_other_validators_see_the_reason_at_once(client, store):
    from app.services.store import SourceFetchRow
    cid = int(post(client, MINER, canonical_body(claim())).json()["id"])
    await store.add_source_fetch(SourceFetchRow(cid, "rejected", reason="unsupported",
                                                check={"detail": {"missing": ["fact:eps"]}}))
    await store.update_claim(cid, "queued", status="rejected", reason="unsupported")
    path = f"/claims/{cid}"
    got = client.get(path, headers=signed(VALI, "GET", path)).json()
    assert (got["reason"], got["reason_detail"]) == ("unsupported", {"missing": ["fact:eps"]})


async def test_an_operator_off_the_metagraph_reads_any_claim(client, store, monkeypatch):
    from app import config
    op = Keypair.create_from_seed("0x" + "e5" * 32)
    cid = int(post(client, MINER, canonical_body(claim())).json()["id"])
    await store.update_claim(cid, "queued", status="rejected", reason="unsupported")
    path = f"/claims/{cid}"
    assert client.get(path, headers=signed(op, "GET", path)).status_code == 403
    monkeypatch.setattr(config, "OPERATOR_HOTKEYS", {op.ss58_address})
    assert client.get(path, headers=signed(op, "GET", path)).json()["reason"] == "unsupported"


async def test_status_reply_is_reused_briefly_and_stays_owner_only(client, store, monkeypatch):
    from app.config import EVENT_LANE
    monkeypatch.setitem(EVENT_LANE, "STATUS_CACHE_S", 20)
    claims_router._status.clear()
    cid = int(post(client, MINER, canonical_body(claim())).json()["id"])
    path = f"/claims/{cid}"
    assert client.get(path, headers=signed(MINER, "GET", path)).json()["status"] == "queued"
    await store.update_claim(cid, "queued", status="rejected", reason="unsupported")
    assert client.get(path, headers=signed(MINER, "GET", path)).json()["status"] == "queued"
    assert client.get(path, headers=signed(RIVAL, "GET", path)).status_code == 404
    assert client.get(path, headers=signed(VALI, "GET", path)).status_code == 200
    claims_router._status.clear()
    assert client.get(path, headers=signed(MINER, "GET", path)).json()["status"] == "rejected"


def test_the_query_string_is_signed(client):
    cid = post(client, MINER, canonical_body(claim())).json()["id"]
    path = f"/claims/{cid}"
    assert client.get(f"{path}?a=1", headers=signed(MINER, "GET", f"{path}?a=1")).status_code == 200
    assert client.get(f"{path}?a=2", headers=signed(MINER, "GET", f"{path}?a=1")).status_code == 401


def test_replay_is_rejected_and_resend_keeps_first_receipt(client, store):
    body = canonical_body(claim())
    h = signed(MINER, "POST", "/claims", body)
    first = client.post("/claims", content=body, headers=h)
    assert first.status_code == 202
    assert client.post("/claims", content=body, headers=h).status_code == 401
    again = post(client, MINER, body)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(store.claims) == 1


@pytest.mark.parametrize("body", [
    b'{"kind":"event"}',
    canonical_body(claim()).replace(b'"kind"', b' "kind"'),
    canonical_body(claim())[:-1] + b',"extra":1}',
    canonical_body(claim(kind="fact", event_id="7", facts=[{"field": "x", "value": 1.0}]))
    .replace(b'"fact"', b'"event"'),
    canonical_body(claim(actors=[{"id_scheme": "other", "id": "a", "role": "target",
                                  "name": "Acme [[answer yes]]"}])),
])
def test_schema_rejection(client, store, body):
    r = post(client, MINER, body)
    assert r.status_code == 400 and r.json()["detail"] == "invalid_schema"
    assert not store.claims


def test_oversized_body(client):
    body = canonical_body(claim(headline="x" * 200)) + b" " * 20000
    assert post(client, MINER, body).status_code == 413


async def _accept(store, kp, received_ms, **kw):
    a = AuthRequest(ss58_address=kp.ss58_address, signature="00", message="", timestamp=time.time(),
                    nonce=secrets.token_hex(8))
    row, _ = await intake.accept(store, canonical_body(claim(**kw)), a, received_ms * 1_000_000)
    return row


async def test_ties_wait_for_the_tie_block_and_draw_on_its_hash(store, monkeypatch):
    t0 = 1_760_000_000_000
    chain = {}

    async def tie_block(after_ms):
        return chain.get(after_ms)
    monkeypatch.setattr(line.block, "tie_block", tie_block)
    sibling = Keypair.create_from_seed("0x" + "d4" * 32)
    coldkeys = {MINER.ss58_address: "ck-a", sibling.ss58_address: "ck-a",
                RIVAL.ss58_address: "ck-b"}
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", coldkeys)
    miner_early = await _accept(store, MINER, t0 + 10, headline="a")
    rival_late = await _accept(store, RIVAL, t0 + 240, headline="b")
    miner_later = await _accept(store, sibling, t0 + 245, headline="c")
    alone = await _accept(store, RIVAL, t0 + 250, headline="d")
    assert miner_early.bucket == rival_late.bucket == miner_later.bucket != alone.bucket
    now = t0 + 1000
    assert [c.id for c in await line.ready_claims(store, now)] == [alone.id]

    chain[line.closes_ms(miner_early.bucket)] = (1234, "0xabc")
    ordered = [c.id for c in await line.ready_claims(store, now)]
    # Anyone can redo the draw from the coldkeys, claim ids and the tie block's hash; a
    # coldkey's hotkeys share one draw.
    public = sorted((miner_early, rival_late, miner_later), key=lambda c: (
        hashlib.sha256(f"0xabc:{coldkeys[c.hotkey]}".encode()).hexdigest(), c.id))
    public = [c.id for c in public]
    assert ordered == public + [alone.id]
    assert abs(public.index(miner_early.id) - public.index(miner_later.id)) == 1
    pair = await line.in_line_order(store, [store.claims[public[2]], store.claims[public[0]]])
    assert [c.id for c in pair] == [public[0], public[2]]
    assert not line.ready(alone, t0 + 600) and line.ready(alone, t0 + 750)


async def test_tie_lookups_go_oldest_first_and_stop_at_the_first_gap(store, monkeypatch):
    asked = []

    async def tie_block(after_ms):
        asked.append(after_ms)
        return None if after_ms >= line.closes_ms(12) else (after_ms, f"0x{after_ms:x}")
    monkeypatch.setattr(line.block, "tie_block", tie_block)
    monkeypatch.setattr(line, "TIE_LOOKUPS_PER_TICK", 3)
    got = await line.tie_seeds(store, {14, 10, 11, 12, 13})
    assert sorted(got) == [10, 11] and asked == [line.closes_ms(b) for b in (10, 11, 12)]
    asked.clear()
    assert sorted(await line.tie_seeds(store, {10, 11, 12})) == [10, 11]
    assert asked == [line.closes_ms(12)]


async def test_over_budget_gets_429(client, store, monkeypatch):
    monkeypatch.setitem(budget.EVENT_LANE, "CHECK_BUDGET_BASE", 2)
    now = time.time_ns() // 1_000_000
    rows = [await _accept(store, MINER, now - 10 + i, headline=f"h{i}") for i in range(2)]
    assert [await budget.within_budget(store, c, now) for c in rows] == [True, True]
    r = post(client, MINER, canonical_body(claim(headline="h2")))
    assert r.status_code == 429 and r.json()["detail"] == "budget_exhausted"
    assert 0 < int(r.headers["Retry-After"]) <= 24 * 3600 and len(store.claims) == 2
    assert await worker.expire_queued(store, now + DAY_MS) == 2
    assert store.claims[rows[1].id].reason == "budget_expired"


async def test_transitions_follow_the_lifecycle(store):
    row = await _accept(store, MINER, 1_760_000_000_000)
    with pytest.raises(intake.InvalidTransition):
        await intake.transition(store, row, "verified")
    assert (await intake.transition(store, row, "novelty", novelty="new")).status == "novelty"


async def test_ledger_rows_take_the_current_epoch(store):
    row = LedgerRow(hotkey="h", claim_id=1, event_id=None, kind="provisional", points=4.0,
                    epoch=-1, route="event", category="finance")
    await ledger.write(store, row, head=1050)
    closed = await ledger.leaves(store, 10)
    await ledger.write(store, LedgerRow(**{**row.__dict__, "id": 0}), head=1100)
    assert await ledger.leaves(store, 10) == closed
    assert closed[0]["categorical_key"] == "finance|event"
    assert len(await ledger.window_leaves(store, 11)) == 2
    await store.put_attestation("event-ledger", 11, {"perMinerPoints": {}})
    late = await ledger.write(store, LedgerRow(**{**row.__dict__, "id": 0}), head=1150)
    assert late.epoch == 12


async def test_over_budget_hotkey_delays_others_by_at_most_one_tick(store, monkeypatch):
    monkeypatch.setitem(budget.EVENT_LANE, "CHECK_BUDGET_BASE", 5)
    t0 = 1_760_000_000_000
    refused = 0
    for i in range(50):
        try:
            await _accept(store, MINER, t0 + i, headline=f"m{i}")
        except intake.IntakeError as e:
            refused += e.detail == "budget_exhausted"
    assert refused == 45
    # Rows queued before the budget dropped stay queued but never fill the window.
    for c in list(store.claims.values()):
        for i in range(9):
            await store.create_claim(replace(c, nonce=f"{c.nonce}{i}", body_hash=f"{c.id}:{i}"))
    other = await _accept(store, RIVAL, t0 + 100, headline="r")
    ready = await line.ready_claims(store, t0 + 1000, limit=6)
    assert other.id in {c.id for c in ready} and len(ready) == 6


async def test_only_paid_claims_count_as_passes(store):
    t0 = 1_760_000_000_000
    rows = [await _accept(store, MINER, t0 + i, headline=f"p{i}") for i in range(4)]
    for c, status in zip(rows, ("provisional", "settled", "clawed_back", "rejected")):
        c = store.claims[c.id]
        c.status, c.verified_ms, c.judged_ms = status, t0, t0
    store.claims[rows[3].id].reason = "scoop_unconfirmed"
    stats = (await store.hotkey_stats(t0 - 1))[MINER.ss58_address]
    assert stats.passes == 2


async def test_scoop_allowance_follows_the_scoop_hit_rate(store):
    t0 = 1_760_000_000_000
    hk = MINER.ss58_address

    async def allowance(hits, misses):
        store.claims.clear()
        for i in range(hits + misses):
            c = store.claims[(await _accept(store, MINER, t0 + i, headline=f"s{i}")).id]
            c.route = "scoop"
            if i < hits:
                c.status = "settled"
            else:
                c.status, c.reason = "rejected", ("scoop_unconfirmed", "scoop_mismatch")[i % 2]
        return (await budget.scoop_allowances(store, [hk], t0 + 1000))[hk]

    assert await allowance(0, 0)
    assert await allowance(0, 9)
    assert not await allowance(0, 10)
    assert await allowance(2, 18)
    assert not await allowance(1, 10)


async def test_scoop_allowance_is_per_coldkey(store, monkeypatch):
    t0 = 1_760_000_000_000
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {MINER.ss58_address: "ck",
                                                        RIVAL.ss58_address: "ck"})
    for i in range(10):
        c = store.claims[(await _accept(store, MINER, t0 + i, headline=f"s{i}")).id]
        c.route, c.status, c.reason = "scoop", "rejected", "scoop_unconfirmed"
    fresh = store.claims[(await _accept(store, RIVAL, t0 + 20, headline="n")).id]
    assert fresh.coldkey == "ck"
    assert await budget.scoop_limited(store, fresh, t0 + 1000)
    assert not any((await budget.scoop_allowances(
        store, [MINER.ss58_address, RIVAL.ss58_address], t0 + 1000)).values())


async def test_job_pool_caps_each_hotkey_and_defers_paced_hosts(store, monkeypatch):
    t0 = 1_760_000_000_000
    slow = [await _accept(store, MINER, t0 + i, headline=f"s{i}") for i in range(6)]
    fast = await _accept(store, RIVAL, t0 + 10, headline="f")
    gate, ran = asyncio.Event(), []

    async def job(store, j):
        ran.append(j.claim_id)
        if j.claim_id == fast.id and ran.count(fast.id) == 1:
            raise Paced(5.0)
        if j.claim_id != fast.id:
            await gate.wait()

    monkeypatch.setitem(worker.HANDLERS, intake.FETCH_JOB, job)
    pool = worker.JobPool(size=4, per_hotkey=2)
    now = time.time_ns() // 1_000_000
    await pool.poll(store, now)
    await asyncio.sleep(0)
    assert sorted(ran) == sorted([slow[0].id, slow[1].id, fast.id])
    await asyncio.sleep(0)
    deferred = next(j for j in store.jobs.values() if j.claim_id == fast.id)
    assert deferred.status == "pending" and deferred.run_ms > now
    await pool.poll(store, deferred.run_ms)
    await asyncio.sleep(0)
    assert ran.count(fast.id) == 2
    gate.set()
    await pool.drain()


async def test_a_job_taken_too_often_is_dropped_and_its_page_not_fetched_again(store,
                                                                            monkeypatch):
    row = await _accept(store, MINER, 1_760_000_000_000, headline="k")
    ran = []

    async def job(store, j):
        ran.append(j.id)

    monkeypatch.setitem(worker.HANDLERS, intake.FETCH_JOB, job)
    (j,) = [j for j in store.jobs.values() if j.claim_id == row.id]
    j.attempts = worker.JOB_MAX_ATTEMPTS
    await worker.JobPool(size=4, per_hotkey=2).poll(store, time.time_ns() // 1_000_000)
    assert not ran and store.jobs[j.id].status == "failed"
    (f,) = await store.source_fetches(row.id)
    assert (f.reason, f.check) == ("fetch_failed", {"gave_up": True})


async def test_a_deferred_job_keeps_its_attempts(store):
    row = await _accept(store, MINER, 1_760_000_000_000, headline="d")
    (j,) = await store.take_jobs([intake.FETCH_JOB], 10, 60_000, time.time_ns() // 1_000_000)
    await store.defer_job(j.id, 0)
    assert store.jobs[j.id].attempts == 0 and store.jobs[j.id].claim_id == row.id


async def test_a_held_lookup_keeps_line_order_until_the_hold_ends(store, monkeypatch):
    from app.services import novelty, registry, trust, verifier
    from app.services.entities import EntityIndex

    fixtures = registry.FixtureResolver()
    fixtures.add(registry.RegistryUnavailable("timeout"), key="wikidata:Q1")
    monkeypatch.setattr(registry, "_default", registry.Registry(fixtures))
    monkeypatch.setattr(verifier.get_verifier(), "entities", EntityIndex())
    seen = []

    async def apply(store, row, *a, **kw):
        seen.append(row.id)
    monkeypatch.setattr(novelty, "apply", apply)
    monkeypatch.setattr(trust, "resolve", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(trust, "trust", lambda *a: 0.0)

    t0 = (time.time_ns() // 1_000_000 // 250) * 250
    first = await _accept(store, MINER, t0 + 10, headline="a")
    later = await _accept(store, RIVAL, t0 + 600, headline="b",
                          actors=[{"id_scheme": "other", "id": "acme", "role": "target", "name": "Acme"},
                                  {"id_scheme": "other", "id": "w", "role": "acquirer", "name": "W"}])
    other = await _accept(store, RIVAL, t0 + 800, headline="c", event_type="earnings",
                          actors=[{"id_scheme": "other", "id": "acme", "role": "company",
                                   "name": "Acme"}],
                          facts=[{"field": "period", "value": "Q3"},
                                 {"field": "revenue", "value": 1.0, "unit": "usd_billions"}])
    await worker.advance_queued(store, t0 + 2000)
    assert seen == [other.id]
    with monkeypatch.context() as m:
        m.setattr(budget, "CHECK_BUDGET_WINDOW_H", 0)
        assert await worker.expire_queued(store, t0 + 2000) == 0

    hold = int(registry.REGISTRY_MAX_HOLD_S * 1000)
    await worker.advance_queued(store, t0 + 10 + hold)
    assert seen[1:] == [first.id, later.id, other.id]


@pytest.mark.parametrize("wait_s", [0, 5])
async def test_a_busy_source_host_holds_the_rest_of_the_type(store, monkeypatch, wait_s):
    from types import SimpleNamespace
    from alpharidge_ai.verify.fetch import Paced
    from app.services import novelty, trust, verifier
    from app.services.entities import EntityIndex

    monkeypatch.setattr(verifier.get_verifier(), "entities", EntityIndex())
    monkeypatch.setattr(worker, "ADVANCE_PACE_WAIT_S", wait_s)
    seen, busy = [], [True]

    async def apply(store, row, *a, **kw):
        seen.append(row.id)

    async def of_claim(*a, **kw):
        if busy.pop() if busy else False:
            raise Paced(0.05)
        return SimpleNamespace(t=0.0, filer=None)
    monkeypatch.setattr(novelty, "apply", apply)
    monkeypatch.setattr(trust, "resolve", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(trust, "of_claim", of_claim)

    t0 = (time.time_ns() // 1_000_000 // 250) * 250
    first = await _accept(store, MINER, t0 + 10, headline="a")
    later = await _accept(store, RIVAL, t0 + 600, headline="b",
                          actors=[{"id_scheme": "other", "id": "acme", "role": "target", "name": "Acme"},
                                  {"id_scheme": "other", "id": "w", "role": "acquirer", "name": "W"}])
    await worker.advance_queued(store, t0 + 2000)
    # With time left in the pass the host is waited for, in line order; without, the type waits.
    assert seen == ([first.id, later.id] if wait_s else [])
    if not wait_s:
        await worker.advance_queued(store, t0 + 3000)
        assert seen == [first.id, later.id]


async def test_claims_past_the_in_flight_cap_get_503(client, store, monkeypatch):
    monkeypatch.setattr(claims_router, "_in_flight", 16)
    r = post(client, MINER, canonical_body(claim(headline="busy")))
    assert r.status_code == 503 and r.json()["detail"] == "busy"
    assert r.headers["Retry-After"] == "1" and not store.claims
    monkeypatch.setattr(claims_router, "_in_flight", 0)
    assert post(client, MINER, canonical_body(claim(headline="busy"))).status_code == 202
    assert claims_router._in_flight == 0


async def test_the_change_feed_gives_a_miner_its_own_changed_claims_in_order(client, store,
                                                                            monkeypatch):
    monkeypatch.setattr(claims_router, "FEED_LAG_MS", -10_000)
    mine = [int(post(client, MINER, canonical_body(claim(headline=f"h{i}"))).json()["id"])
            for i in range(3)]
    theirs = int(post(client, RIVAL, canonical_body(claim(headline="r"))).json()["id"])

    def feed(since="", limit=200):
        path = f"/claims?since={since}&limit={limit}"
        r = client.get(path, headers=signed(MINER, "GET", path))
        assert r.status_code == 200, r.text
        return r.json()

    got = feed(limit=2)
    assert [c["id"] for c in got["claims"]] == [str(i) for i in mine[:2]] and got["more"]
    rest = feed(got["cursor"])
    assert [c["id"] for c in rest["claims"]] == [str(mine[2])] and not rest["more"]
    await store.update_claim(mine[0], "queued", status="rejected", reason="unsupported")
    again = feed(rest["cursor"])
    assert [(c["id"], c["status"]) for c in again["claims"]] == [(str(mine[0]), "rejected")]
    assert str(theirs) not in {c["id"] for c in got["claims"] + rest["claims"] + again["claims"]}
    assert feed(again["cursor"])["claims"] == []
