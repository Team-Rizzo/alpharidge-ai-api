"""Points: tier prices, the 40/60 split, clawback, holds and signed points."""

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from bittensor_wallet import Keypair

from alpharidge_ai.ledger.attestation import CLOSE_GRACE_BLOCKS
from alpharidge_ai.ledger.pull import LedgerPull
from alpharidge_ai.mechanism.pools import coldkeys, net_by_coldkey
from alpharidge_ai.schema import Claim
from alpharidge_ai.validator.reverify import paid_right
from alpharidge_ai.verify.check import verdict_hash
from alpharidge_ai.verify.dates import parse_datetime
from app import config
from app.config import EVENT_LANE
from app.routers import ledger as ledger_router
from app.services import ledger, points, product, trust
from alpharidge_ai.judge.client import ScriptedJudge
from app.services.answer_key import match
from app.services.store import LedgerRow, MemoryStore

from .pipeline import HEAD, HOUR_MS, T0, Lane
from .test_guards import actors

KP = Keypair.create_from_seed("0x" + "22" * 32)


def test_price_is_the_tier_times_trust_split_40_60():
    f = points.factors("merger_acquisition", "event", 0.5, 1.0)
    assert (f.base, f.product) == (12, 6.0)
    assert points.provisional(f) + points.settled(f) == pytest.approx(6.0)
    assert points.factors("earnings", "event", 1.0, 0.5).product == 1.5


def test_pay_slides_with_age_and_validators_agree():
    day = date(2026, 10, 1)
    end = datetime(2026, 10, 2, tzinfo=timezone.utc)
    ms = lambda h: int((end + timedelta(hours=h)).timestamp() * 1000)
    assert points.age(day, ms(-20)) == points.age(day, ms(48)) == 1.0
    assert points.age(day, ms(15 * 24)) == pytest.approx(1 - 0.75 * (360 - 48) / (720 - 48))
    assert points.age(day, ms(30 * 24)) == pytest.approx(0.25)
    assert points.age(day, ms(40 * 24)) == 0.25

    claim = Claim.model_validate({
        "kind": "event", "headline": "Acme to acquire Beta", "event_type": "merger_acquisition",
        "category": "finance", "event_date": day.isoformat(),
        "actors": [{"id_scheme": "wikidata", "id": "Q1", "role": "acquirer", "name": "Acme"}],
        "facts": [], "source_url": "https://newsone.com/x", "quote": "Acme agreed to buy Beta."})
    received = end + timedelta(days=15)
    f = points.factors(claim.event_type, "event", 0.8, points.age(day, ms(15 * 24)))
    item = {"route": "event", "points": points.provisional(f), "factors": f.as_dict()}
    assert paid_right(item, claim, received)
    assert not paid_right({**item, "factors": {**f.as_dict(), "A": 1.0}}, claim, received)
    assert not paid_right(item, claim, received + timedelta(days=1))

    # An earlier publish day sets the age; a later one does not.
    pub = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    a = points.age(day, ms(15 * 24), int(pub.timestamp() * 1000))
    assert a == points.age(date(2026, 9, 25), ms(15 * 24)) < points.age(day, ms(15 * 24))
    assert points.age(day, ms(15 * 24), ms(0)) == points.age(day, ms(15 * 24))
    f = points.factors(claim.event_type, "event", 0.8, a)
    item = {"route": "event", "points": points.provisional(f), "factors": f.as_dict(),
            "published_at": pub.isoformat()}
    assert paid_right(item, claim, received)
    assert not paid_right({**item, "published_at": None}, claim, received)


async def one_event(lane: Lane):
    await lane.sources()
    row = await lane.submit("m1", T0 + 60_000, source_url="https://newsone.com/x")
    await lane.step(T0 + 61_000)
    return lane.store.claims[row.id]


async def test_clawback_takes_back_everything_paid():
    lane = Lane(check=True)
    row = await one_event(lane)
    await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD)
    row = await lane.store.get_claim(row.id)
    assert row.status == "settled" and lane.paid()["m1"] > 0
    await ledger.clawback(lane.store, [row], "clawed:retraction", head=HEAD)
    assert lane.paid()["m1"] == pytest.approx(0.0)
    assert (await lane.store.get_claim(row.id)).status == "clawed_back"


async def test_settle_and_clawback_racing_leave_nothing_paid():
    lane = Lane(check=True)
    row = await one_event(lane)
    append = lane.store.append_ledger

    async def slow_append(r):
        await asyncio.sleep(0)
        return await append(r)
    lane.store.append_ledger = slow_append
    await asyncio.gather(points.settle_one(lane.store, row, head=HEAD),
                         ledger.clawback(lane.store, [row], "clawed:retraction", head=HEAD))
    assert (await lane.store.get_claim(row.id)).status == "clawed_back"
    assert lane.paid()["m1"] == pytest.approx(0.0)


async def test_settle_waits_while_the_chain_head_is_unavailable(monkeypatch):
    lane = Lane(check=True)
    row = await one_event(lane)

    async def no_head():
        return None
    monkeypatch.setattr(points.block, "current_block", no_head)
    with pytest.raises(RuntimeError):
        await points.settle_due(lane.store, T0 + 73 * HOUR_MS)
    assert (await lane.store.get_claim(row.id)).status == "provisional"
    assert await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD) == 1
    assert [r.kind for r in lane.store.ledger].count(ledger.SETTLE) == 1


async def test_a_late_claw_nets_only_rows_still_in_the_window(monkeypatch):
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"vali"})
    lane = Lane(check=True)
    row = await one_event(lane)
    blocks = EVENT_LANE["BLOCK_LENGTH"]
    await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD + 216 * blocks)
    row = await lane.store.get_claim(row.id)
    settle = [r for r in lane.store.ledger if r.kind == ledger.SETTLE]
    assert row.status == "settled" and len(settle) == 1
    assert match.clawable(row, row.settle_ms + 1)
    assert not match.clawable(row, row.settle_ms + ledger.window_ms() + 1)
    await ledger_router.record_reverify(lane.store, row.id, "vali", 1, "disagree", {})
    row = await lane.store.get_claim(row.id)
    assert row.held
    await points.resolve_held(lane.store, row, "claw", head=HEAD + 250 * blocks)
    (claw,) = [r for r in lane.store.ledger if r.kind == ledger.CLAWBACK]
    assert claw.points == pytest.approx(-settle[0].points)
    row = await lane.store.get_claim(row.id)
    assert row.status == "clawed_back"


async def test_disagreement_holds_the_settle_until_cleared(monkeypatch):
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"vali"})
    lane = Lane(check=True)
    row = await one_event(lane)
    await ledger_router.record_reverify(lane.store, row.id, "other", 1, "disagree", {})
    assert not (await lane.store.get_claim(row.id)).held
    await ledger_router.record_reverify(lane.store, row.id, "vali", 1, "disagree", {})
    assert await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD) == 0
    await points.resolve_held(lane.store, await lane.store.get_claim(row.id), "clear")
    assert await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD) == 1


class _Client:
    def __init__(self, store, body):
        self.store, self.body = store, body

    async def ledger_points(self, epoch):
        return self.body

    async def ledger_leaves(self, epoch, cursor=None, limit=5000):
        return {"leaves": await ledger.leaves(self.store, epoch), "next_cursor": None}


async def test_signed_points_verify_on_the_validator_side():
    lane = Lane(check=True)
    await one_event(lane)
    epoch = ledger.epoch_of(HEAD)
    p = ledger.defaults()
    body = await ledger.signed_points(lane.store, epoch, p, keypair=KP)
    assert body == await ledger.signed_points(lane.store, epoch, replace(p, capacity=1.0),
                                              keypair=KP)
    pull = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    got, reason = await pull.pull(epoch, {"m1": 3})
    assert reason == "accepted" and got.points["m1"] > 0
    assert (await ledger.leaves(lane.store, epoch))[0]["leaf_version"] == ledger.LEAF_V2

    lane.store.ledger[0].points += 1
    stale = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    assert (await stale.pull(epoch, {"m1": 3}))[1] == "root_mismatch"
    other = Keypair.create_from_seed("0x" + "33" * 32)
    wrong = LedgerPull(_Client(lane.store, body), pinned_pubkey=other.ss58_address)
    assert (await wrong.pull(epoch, {"m1": 3}))[1] == "bad_signature"


@pytest.mark.parametrize("kind", [trust.ANSWER_KEY, trust.CONTRADICT, trust.RETRACTION])
async def test_a_wrong_claim_is_clawed_to_zero_without_a_penalty(kind):
    lane = Lane(check=True)
    row = await one_event(lane)
    await trust.proved_wrong(lane.store, kind, row, "found wrong", "test", T0 + HOUR_MS, HEAD)
    row = lane.store.claims[row.id]
    assert row.status == "clawed_back"
    assert not any(r.kind == ledger.PENALTY for r in lane.store.ledger)
    assert lane.paid()["m1"] == pytest.approx(0)


async def _debt(lane, row):
    """A negative row on a paid claim."""
    paid = [r for r in lane.store.ledger if r.claim_id == row.id]
    await ledger.write(lane.store, LedgerRow(
        hotkey=row.hotkey, claim_id=row.id, event_id=row.event_id, kind=ledger.PENALTY,
        points=-4 * sum(r.points for r in paid), epoch=0, route=paid[0].route,
        category=paid[0].category, reason="test"), HEAD)


async def test_debt_nets_against_a_sibling_hotkey():
    lane = Lane(check=True)
    await lane.sources()
    await lane.submit("m1", T0 + 60_000, source_url="https://newsone.com/x")
    wrong = await lane.submit("m2", T0 + 60_000, headline="Gamma to buy Delta",
                              actors=actors("Q3", "Q4"), source_url="https://wiretwo.org/y")
    await lane.step(T0 + 61_000)
    await _debt(lane, lane.store.claims[wrong.id])
    epoch = ledger.epoch_of(HEAD)
    body = await ledger.signed_points(lane.store, epoch, ledger.defaults(), keypair=KP)
    pull = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    got, reason = await pull.pull(epoch, {"m1": 1, "m2": 2})
    assert reason == "accepted" and got.points == body["attestation"]["perMinerPoints"]
    assert got.points["m2"] < 0 < got.points["m1"] < -got.points["m2"]
    assert net_by_coldkey(got.points, {}) == {"m1": got.points["m1"]}
    assert net_by_coldkey(got.points, {"m1": "ck", "m2": "ck"}) == {}
    settles = await ledger_router.settles(lane.store, await lane.store.ledger_rows([epoch]))
    assert await pull.unbound_settles(epoch, settles) is None


async def test_signed_coldkey_nets_a_deregistered_hotkeys_debt():
    lane = Lane(check=True)
    await lane.sources()
    await lane.submit("m1", T0 + 60_000, coldkey="ck", source_url="https://newsone.com/x")
    wrong = await lane.submit("m2", T0 + 60_000, coldkey="ck", headline="Gamma to buy Delta",
                              actors=actors("Q3", "Q4"), source_url="https://wiretwo.org/y")
    await lane.step(T0 + 61_000)
    await _debt(lane, lane.store.claims[wrong.id])
    epoch = ledger.epoch_of(HEAD)
    leaves = await ledger.leaves(lane.store, epoch)
    assert {leaf["coldkey"] for leaf in leaves} == {"ck"}
    body = await ledger.signed_points(lane.store, epoch, ledger.defaults(), keypair=KP)
    pull = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    got, reason = await pull.pull(epoch, {"m1": 1})
    assert reason == "accepted" and got.coldkeys == {"m1": "ck", "m2": "ck"}
    assert got.points["m2"] < 0 < got.points["m1"]
    assert net_by_coldkey(got.points, {"m1": "other", **got.coldkeys}) == {}
    lane.store.ledger[0].coldkey = "forged"
    fresh = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    assert (await fresh.pull(epoch, {"m1": 1}))[1] == "root_mismatch"
    assert coldkeys([{**leaves[0], "leaf_version": None}]) == {}


class _Signer:
    def __init__(self, store, p):
        self.store, self.p = store, p

    async def ledger_points(self, epoch):
        return await ledger.signed_points(self.store, epoch, self.p, keypair=KP)

    async def ledger_leaves(self, epoch, cursor=None, limit=5000):
        return {"leaves": await ledger.leaves(self.store, epoch), "next_cursor": None}


async def _debt_store():
    """A 100-point debt, then 10 points an epoch on a sibling hotkey."""
    store = MemoryStore()
    await store.append_ledger(LedgerRow("m2", None, None, ledger.PENALTY, -100.0, 0, "event",
                                        "finance", received_ms=1, bucket=0, coldkey="ck"))
    for e in range(1, 12):
        await store.append_ledger(LedgerRow("m1", None, None, ledger.PROVISIONAL, 10.0, e,
                                            "event", "finance", received_ms=e, bucket=0,
                                            coldkey="ck"))
    return store


async def _balance(pull, epoch):
    got, reason = await pull.pull(epoch, {"m1": 1, "m2": 2})
    assert reason == "accepted"
    return sum(got.points.values())


async def test_debt_beyond_a_window_carries_until_paid_off():
    p = replace(ledger.defaults(), window=3)
    store = await _debt_store()
    pull = LedgerPull(_Signer(store, p), pinned_pubkey=KP.ss58_address, window=3)
    balances = [await _balance(pull, e) for e in range(12)]
    assert balances[:3] == [-100, -90, -80]
    assert balances[3:6] == [-50] * 3 and balances[6:9] == [-20] * 3
    assert balances[9:] == [10] * 3
    assert [(r.epoch, r.points) for r in store.ledger if r.kind == ledger.CARRY] == [
        (3, -80), (6, -50), (9, -20)]


async def test_a_forged_carry_fails_on_the_validator_side():
    p = replace(ledger.defaults(), window=3)
    store = await _debt_store()
    for e in range(3):
        await ledger.signed_points(store, e, p, keypair=KP)
    await ledger.carry(store, 3, p.window)
    (row,) = [r for r in store.ledger if r.kind == ledger.CARRY]
    row.points = -1.0
    pull = LedgerPull(_Signer(store, p), pinned_pubkey=KP.ss58_address, window=3)
    assert (await pull.pull(3, {"m1": 1}))[1] == "carry_mismatch"


async def test_points_requests_sign_a_bounded_range(monkeypatch):
    p = replace(ledger.defaults(), window=3)
    store = MemoryStore()
    for bad in (-1, 4):
        with pytest.raises(ledger.EpochTooOld):
            await ledger.signed_points(store, bad, p, keypair=KP, oldest=5)
    await ledger.signed_points(store, 5, p, keypair=KP, oldest=5)
    monkeypatch.setattr(ledger, "SIGN_CATCHUP_MAX", 2)
    with pytest.raises(RuntimeError, match="signing_behind"):
        await ledger.signed_points(store, 10, p, keypair=KP)
    assert await store.last_attestation_epoch(ledger.LEDGER_SIGNER) == 7
    await ledger.signed_points(store, 10, p, keypair=KP)
    assert await store.last_attestation_epoch(ledger.LEDGER_SIGNER) == 10
    assert await store.get_attestation(ledger.LEDGER_SIGNER, 4) is None
    with pytest.raises(ledger.EpochTooOld):
        await ledger.signed_points(store, 4, p, keypair=KP, oldest=0)
    assert (await ledger.signed_points(store, 6, p, keypair=KP, oldest=9))["epoch"] == 6


async def test_settle_rows_are_served_with_their_paid_leaf():
    lane = Lane(check=True)
    row = await one_event(lane)
    later = HEAD + 5 * EVENT_LANE["BLOCK_LENGTH"]
    await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=later)
    await ledger.clawback(lane.store, [await lane.store.get_claim(row.id)], "clawed:retraction",
                          head=later)
    epoch = ledger.epoch_of(later)
    settles = await ledger_router.settles(lane.store, await lane.store.ledger_rows([epoch]))
    assert [s["paid_epoch"] for s in settles] == [ledger.epoch_of(HEAD)] * 2
    body = await ledger.signed_points(lane.store, epoch, ledger.defaults(), keypair=KP)
    pull = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    assert await pull.unbound_settles(epoch, settles) is None
    assert await pull.unbound_settles(epoch, settles[:1]) == "unbound_settle"


async def test_sample_lists_paid_claims_of_the_epoch():
    lane = Lane(check=True)
    row = await one_event(lane)
    assert await ledger_router.candidates(lane.store, ledger.epoch_of(HEAD)) == [row.id]
    assert await ledger_router.candidates(MemoryStore(), ledger.epoch_of(HEAD)) == []


async def test_ledger_endpoints(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import claims as claims_router
    from app.utils import attestation_crypto, block, mechanism_profile

    lane = Lane(check=True)
    row = await one_event(lane)
    epoch = ledger.epoch_of(HEAD)

    async def head():
        return HEAD + EVENT_LANE["BLOCK_LENGTH"] + CLOSE_GRACE_BLOCKS

    async def no_profile():
        return {}

    monkeypatch.setattr(block, "current_block", head)
    monkeypatch.setattr(mechanism_profile, "load_profiles", no_profile)
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"vali"})
    monkeypatch.setattr(attestation_crypto, "load_signing_key", lambda: KP)
    app = FastAPI()
    app.include_router(ledger_router.router)
    app.dependency_overrides[claims_router.get_store] = lambda: lane.store
    caller = {"hk": "vali"}
    app.dependency_overrides[ledger_router.get_validator_hotkey] = lambda: caller["hk"]
    client = TestClient(app)

    assert client.get(f"/ledger/points?epoch={epoch + 1}").status_code == 409
    body = client.get(f"/ledger/points?epoch={epoch}").json()
    att = body["attestation"]
    msg = attestation_crypto.attestation_message(
        "event-ledger", epoch, att["perMinerPoints"], att["totalPoints"], att["merkleRoot"])
    assert attestation_crypto.verify_attestation(KP.ss58_address, msg, body["signature"])
    assert body["capacity"] == ledger.CAPACITY and body["leaf_count"] == 1
    leaves = client.get(f"/ledger/leaves?epoch={epoch}").json()
    assert leaves["leaves"][0]["claim_id"] == str(row.id) and leaves["next_cursor"] is None

    url = f"/ledger/sample?epoch={epoch}&validator="
    assert client.get(url + "other").status_code == 403
    (item,) = client.get(url + "vali").json()["items"]
    assert item["claim_id"] == str(row.id) and item["route"] == "event" and item["content_hash"]
    record = item["judge"]
    assert record["outcome"] == "pass" and record["content_hash"] == item["content_hash"]
    assert item["verdict_hash"] == verdict_hash(record) == leaves["leaves"][0]["verdict_hash"]
    assert paid_right(item, Claim.model_validate_json(item["body"]),
                      parse_datetime(item["received_at"]))
    assert lane.store.pulls and lane.store.pulls[0][:2] == ("vali", epoch)
    # A validator off the re-verify list never gets the page or body.
    caller["hk"] = "late"
    monkeypatch.setattr(ledger_router, "_now_ms", lambda: row.settle_ms - 1)
    (early,) = client.get(url + "late").json()["items"]
    assert early["withheld"] and not {"body", "source_url", "final_url", "judge"} & set(early)
    assert early["body_sha256"] == row.body_hash and early["points"] == item["points"]
    monkeypatch.setattr(ledger_router, "_now_ms", lambda: row.settle_ms)
    (late,) = client.get(url + "late").json()["items"]
    assert late["withheld"] and not {"body", "source_url", "final_url", "judge"} & set(late)
    caller["hk"] = "vali"

    bad = {"epoch": epoch, "results": [{"claim_id": "999", "verdict": "agree"}]}
    assert client.post("/ledger/reverify", json=bad).status_code == 400
    assert client.post("/ledger/reverify", json={"epoch": epoch + 1, "results": []}
                       ).status_code == 409
    ok = {"epoch": epoch, "results": [{"claim_id": str(row.id), "verdict": "disagree",
                                       "observed": {"verdict": "contradicted"}}]}
    assert client.post("/ledger/reverify", json=ok).json() == {"accepted": 1}
    assert (await lane.store.get_claim(row.id)).held
    assert ("vali", epoch) in lane.store.reports
    assert ledger_router.result_cap(0) == ledger_router.REVERIFY_CAP_MIN
    assert ledger_router.result_cap(10_000) == 20_000


def test_results_cap_follows_the_reverify_rate_env_then_the_profile(monkeypatch):
    from alpharidge_ai.schema.types import reverify_rate
    monkeypatch.setenv("LEDGER_REVERIFY_RATE", "0.1")
    monkeypatch.setattr(ledger, "_params", None)
    assert reverify_rate() == 0.1 and ledger_router.result_cap(10_000) == 2_000
    signed = ledger.params_from({"ledger": {"reverify_rate": 0.5, "settle_delay_h": 48}})
    monkeypatch.setattr(ledger, "_params", signed)
    assert ledger_router.result_cap(10_000) == 10_000
    assert product.settle_ms() == 48 * 3_600_000


class Retyping(ScriptedJudge):
    """A scripted judge that does not pass one type and picks another for the page."""

    def __init__(self, claimed, kind):
        super().__init__()
        self.claimed, self.kind = claimed, kind

    def checks(self, claim, text):
        out = super().checks(claim, text)
        if claim.event_type.value == self.claimed:
            out.update(event="no", type_answer={"kind": self.kind, "p": 0.9})
        return out


async def test_a_claim_the_judge_retypes_is_kept_as_that_type():
    lane = Lane(check=True, judge=Retyping("trading_halt", "delisting"))
    await lane.sources()
    row = await lane.submit("m1", T0 + 60_000, source_url="https://newsone.com/x",
                            event_type="trading_halt", headline="Acme halted",
                            actors=[{"id_scheme": "wikidata", "id": "Q1", "role": "subject",
                                     "name": "Acme Inc."}],
                            facts=[{"field": "exchange", "value": "Nasdaq"}],
                            quote="Acme Inc. shares will be removed from Nasdaq.")
    await lane.step(T0 + 61_000)
    row = lane.store.claims[row.id]
    assert (row.status, row.event_type, row.payload["event_type"]) == (
        "provisional", "delisting", "delisting")
    assert json.loads(row.body)["event_type"] == "trading_halt"
    assert row.payload["actors"][0]["role"] == "issuer"
    rec = (await lane.store.source_fetches(row.id))[-1].check["judge"]
    assert rec["outcome"] == "pass" and rec["retyped"] == {"kind": "delisting", "first": "reject"}
    assert (await lane.store.get_event(row.event_id)).event_type == "delisting"

    # A type the claim cannot hold (a deal's two parties as one company's results) is unclear.
    lane = Lane(check=True, judge=Retyping("merger_acquisition", "earnings"))
    await lane.sources()
    row = await lane.submit("m1", T0 + 60_000, source_url="https://newsone.com/y")
    await lane.step(T0 + 61_000)
    row = lane.store.claims[row.id]
    assert (row.status, row.reason, row.event_type) == (
        "rejected", "judge_unclear", "merger_acquisition")


async def test_misconduct_penalty_is_five_times_the_value_and_nets_across_the_coldkey():
    from app.services import ops
    lane = Lane(check=True)
    await lane.sources()
    await lane.submit("m1", T0 + 60_000, coldkey="ck", source_url="https://newsone.com/x")
    bad = await lane.submit("m2", T0 + 60_000, coldkey="ck", headline="Gamma to buy Delta",
                            actors=actors("Q3", "Q4"), source_url="https://wiretwo.org/y")
    await lane.step(T0 + 61_000)
    bad = lane.store.claims[bad.id]
    paid = sum(r.points for r in lane.store.ledger if r.claim_id == bad.id)
    got = await ops.penalise(lane.store, bad, "page edited to fit", "op1", T0 + HOUR_MS, HEAD)
    assert got["clawed"] and got["penalty"] == pytest.approx(
        -ledger.MISCONDUCT_PENALTY_X * paid / ledger.PROVISIONAL_SHARE)
    assert await ops.penalise(lane.store, lane.store.claims[bad.id], "again", "op1",
                              T0 + HOUR_MS, HEAD) is None
    assert lane.store.claims[bad.id].status == "clawed_back"
    (log,) = [e for e in await lane.store.trust_log() if e.field == "penalty:misconduct"]
    assert (log.reason, log.actor) == ("page edited to fit", "op1")

    epoch = ledger.epoch_of(HEAD)
    body = await ledger.signed_points(lane.store, epoch, ledger.defaults(), keypair=KP)
    pull = LedgerPull(_Client(lane.store, body), pinned_pubkey=KP.ss58_address)
    got, reason = await pull.pull(epoch, {"m1": 1, "m2": 2})
    assert reason == "accepted" and got.coldkeys == {"m1": "ck", "m2": "ck"}
    assert got.points["m2"] < -got.points["m1"] < 0
    assert net_by_coldkey(got.points, got.coldkeys) == {}
    settles = await ledger_router.settles(lane.store, await lane.store.ledger_rows([epoch]))
    assert await pull.unbound_settles(epoch, settles) is None
