"""A claim's status move and the ledger rows that go with it commit together, so a failure
leaves the claim where it was for the next pass to finish; a claim decided against is never
placed or paid; a pay is never written twice. Tests taking `store` run on the memory store
and, with STORE_TEST_DATABASE_URL set, on Postgres."""

import asyncio
import json
import uuid
from dataclasses import replace

import pytest

from alpharidge_ai.schema import Claim, canonical_body
from alpharidge_ai.verify.check import REJECTED, SUPPORTED
from app.services import intake, ledger, novelty, points, quality, resolve, scoops
from app.services.store import LedgerRow, SourceFetchRow
from tests.pipeline import HEAD, HOUR_MS, T0, Lane, payload
from tests.test_guards import START, actors, deal_page, found
from tests.test_pay_recovery import _fail_once
from tests.test_store_contract import claim, store  # noqa: F401 (fixture)

F = points.Factors(base=12, T=1.0)


@pytest.fixture(autouse=True)
def _fresh_quality():
    quality.reset()
    yield
    quality.reset()


def _fail_rows(monkeypatch, store, kind, times=1):
    """The next `times` appends of a `kind` row fail."""
    real, left = type(store).append_ledger, [times]

    async def append(self, row):
        if row.kind == kind and left[0] > 0:
            left[0] -= 1
            raise RuntimeError(f"{kind} row not written")
        return await real(self, row)

    monkeypatch.setattr(type(store), "append_ledger", append)


async def _paid(store):
    row = await store.create_claim(claim("atomic-" + uuid.uuid4().hex[:10]))
    assert await store.update_claim(row.id, "received", status="verified")
    return await points.pay(store, await store.get_claim(row.id), "event", F, T0, HEAD)


def _kinds(rows):
    return [r.kind for r in rows]


# Settle, clawback, debt

async def test_a_settle_whose_row_fails_leaves_the_claim_provisional(store, monkeypatch):
    row = await _paid(store)
    _fail_rows(monkeypatch, store, ledger.SETTLE)
    with pytest.raises(RuntimeError):
        await points.settle_one(store, row, HEAD)
    got = await store.get_claim(row.id)
    assert got.status == "provisional"
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL]
    got = await points.settle_one(store, got, HEAD)
    assert got.status == "settled"
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL, ledger.SETTLE]


async def test_settle_due_settles_again_a_claim_whose_settle_failed(monkeypatch):
    lane = Lane(check=True)
    await lane.sources()
    rows = [await lane.submit(f"m{i}", T0 + 60_000, source_url=f"https://newsone.com/{i}",
                              headline=f"Deal {i}", actors=actors(f"QS{i}1", f"QS{i}2"))
            for i in range(2)]
    await lane.step(T0 + 61_000)
    assert [lane.store.claims[r.id].status for r in rows] == ["provisional"] * 2
    _fail_rows(monkeypatch, lane.store, ledger.SETTLE)
    # One fails; the pass goes on to the other.
    assert await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD) == 1
    assert sorted(lane.store.claims[r.id].status for r in rows) == ["provisional", "settled"]
    assert await points.settle_due(lane.store, T0 + 73 * HOUR_MS, head=HEAD) == 1
    assert [lane.store.claims[r.id].status for r in rows] == ["settled"] * 2
    assert _kinds(lane.store.ledger).count(ledger.SETTLE) == 2


async def test_a_clawback_whose_row_fails_leaves_the_claim_as_it_was(store, monkeypatch):
    row = await _paid(store)
    _fail_rows(monkeypatch, store, ledger.CLAWBACK)
    with pytest.raises(RuntimeError):
        await ledger.clawback(store, [row], "clawed:test", HEAD)
    got = await store.get_claim(row.id)
    assert got.status == "provisional"
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL]
    assert await ledger.clawback(store, [got], "clawed:test", HEAD) == 1
    got = await store.get_claim(row.id)
    assert (got.status, got.reason) == ("clawed_back", "clawed:test")
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL, ledger.CLAWBACK]


async def test_a_clawback_whose_move_fails_writes_no_row(store, monkeypatch):
    row = await _paid(store)
    real = intake.transition

    async def broken(st, c, to, **kw):
        if to == "clawed_back":
            raise RuntimeError("move failed")
        return await real(st, c, to, **kw)

    monkeypatch.setattr(intake, "transition", broken)
    with pytest.raises(RuntimeError):
        await ledger.clawback(store, [row], "clawed:test", HEAD)
    assert (await store.get_claim(row.id)).status == "provisional"
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL]


async def _held_scoop(store):
    """A verified scoop whose window has ended, with its page fetched."""
    body = canonical_body(Claim(**payload()))
    row = await store.create_claim(replace(claim("atomic-" + uuid.uuid4().hex[:10]),
                                           body=body.decode(), payload=json.loads(body)))
    assert await store.update_claim(row.id, "received", status="verified", route="scoop")
    await store.add_source_fetch(SourceFetchRow(claim_id=row.id, verdict=SUPPORTED,
                                                published_ms=T0))
    return await store.get_claim(row.id)


async def test_a_scoop_closed_unconfirmed_is_rejected_with_its_debt_or_not_at_all(store,
                                                                                  monkeypatch):
    scoop = await _held_scoop(store)
    end = scoop.received_ms + int(novelty.SCOOP_WINDOW_H * HOUR_MS)
    r = resolve.Resolver()
    _fail_rows(monkeypatch, store, ledger.DEBT)
    with pytest.raises(RuntimeError):
        await r._close(store, scoop, end, HEAD)
    assert (await store.get_claim(scoop.id)).status == "verified"
    assert await store.claim_ledger(scoop.id) == []
    await r._close(store, scoop, end, HEAD)
    got = await store.get_claim(scoop.id)
    assert (got.status, got.reason) == ("rejected", "scoop_unconfirmed")
    assert _kinds(await store.claim_ledger(scoop.id)) == [ledger.DEBT]


async def test_a_failed_scoop_is_rejected_with_its_debt_or_not_at_all(store, monkeypatch):
    scoop = await _held_scoop(store)
    r = resolve.Resolver()
    _fail_rows(monkeypatch, store, ledger.DEBT)
    with pytest.raises(RuntimeError):
        await r.scoop_failed(store, scoop, "scoop_mismatch", None, T0, HEAD)
    assert (await store.get_claim(scoop.id)).status == "verified"
    await r.scoop_failed(store, scoop, "scoop_mismatch", None, T0, HEAD)
    got = await store.get_claim(scoop.id)
    assert (got.status, got.reason) == ("rejected", "scoop_mismatch")
    assert _kinds(await store.claim_ledger(scoop.id)) == [ledger.DEBT]


async def test_a_penalty_is_written_once(store):
    row = await _paid(store)
    first = await ledger.penalty(store, row, "penalty:test", HEAD)
    assert first < 0 and await ledger.penalty(store, row, "penalty:test", HEAD) == 0.0
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL, ledger.PENALTY]


async def test_two_penalties_at_once_write_one_row(store):
    """As from two processes: each finds no penalty before its transaction, and the claim's
    row lock lets only the first write one."""
    row = await _paid(store)
    got = await asyncio.gather(*(ledger.penalty(store, row, "penalty:test", HEAD)
                                 for _ in range(3)))
    assert sorted(g < 0 for g in got) == [False, False, True]
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL, ledger.PENALTY]


# A claim whose check failed

async def test_a_claim_whose_check_failed_is_rejected_in_one_move(monkeypatch):
    """The reject is the only move: when it fails, the claim stays in novelty and is checked
    again, never left `fetched` for the resolver to pay."""
    lane = Lane(check=False)
    await lane.sources()
    real, moves = intake.transition, []

    async def flaky(st, c, to, **kw):
        moves.append((c.status, to))
        if to == "rejected" and len(moves) == 2:
            raise RuntimeError("Timed out fetching a new connection from the connection pool")
        return await real(st, c, to, **kw)

    monkeypatch.setattr(intake, "transition", flaky)
    row = await lane.submit("m1", T0, coldkey="ck")
    with pytest.raises(RuntimeError):
        await lane.step(T0 + 1000)
    assert lane.store.claims[row.id].status == "novelty"
    await lane.step(T0 + 2000)
    got = lane.store.claims[row.id]
    assert ("novelty", "fetched") not in moves
    assert got.status == "rejected" and got.judged_ms is not None
    assert await lane.store.claim_ledger(row.id) == []


async def test_the_resolver_rejects_a_claim_whose_check_failed(monkeypatch):
    """A claim left `fetched` although its page check failed (as the old two-step reject
    could leave it) is rejected for the check's reason, not placed or paid."""
    lane = Lane(check=True)
    await lane.sources()
    row = await lane.submit("m1", T0, coldkey="ck")
    real = lane.resolver.resolve
    monkeypatch.setattr(lane.resolver, "resolve", lambda *a, **k: asyncio.sleep(0, a[1]))
    await lane.step(T0 + 1000)
    assert lane.store.claims[row.id].status == "fetched"
    (f,) = [f for f in await lane.store.source_fetches(row.id) if "date_gate" in (f.check or {})]
    await lane.store.update_source_fetch(f.id, verdict=REJECTED, reason="judge_reject")
    monkeypatch.setattr(lane.resolver, "resolve", real)
    await lane.step(T0 + 2000)
    got = lane.store.claims[row.id]
    assert (got.status, got.reason) == ("rejected", "judge_reject")
    assert got.judged_ms is not None and await lane.store.claim_ledger(row.id) == []


# Pay once

async def test_two_pays_of_one_claim_write_one_row(store):
    row = await store.create_claim(claim("atomic-" + uuid.uuid4().hex[:10]))
    assert await store.update_claim(row.id, "received", status="verified")
    row = await store.get_claim(row.id)
    got = await asyncio.gather(points.pay(store, row, "event", F, T0, HEAD),
                               points.pay(store, row, "event", F, T0, HEAD),
                               return_exceptions=True)
    assert sum(isinstance(g, intake.InvalidTransition) for g in got) == 1
    assert _kinds(await store.claim_ledger(row.id)) == [ledger.PROVISIONAL]
    assert (await store.get_claim(row.id)).status == "provisional"


async def _half_paid_scoop(monkeypatch):
    """A confirmed scoop as the pay before it was one transaction could leave it: its paid
    row written, the claim still verified."""
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="R deal", actors=actors("QR1", "QR2"))
    s = await lane.submit("m4", START, source_url="https://lowblog.net/R", **kw)
    await lane.step(START + 1000)
    _fail_once(monkeypatch)
    with pytest.raises(RuntimeError):
        await found(lane, s, "https://wiretwo.org/R", START - 1000, START + 70_000,
                    deal_page("Company QR1", "Company QR2"))
    await lane.store.append_ledger(LedgerRow(
        hotkey="m4", claim_id=s.id, event_id=None, kind=ledger.PROVISIONAL, points=4.0,
        epoch=1, route="scoop", category="finance", factors={"base": 10.0, "T": 1.0}))
    for cand in await lane.store.scoop_candidates(s.id, (scoops.QUEUED,)):
        await lane.store.update_scoop_candidate(cand.id, status=scoops.REJECTED, reason="x")
    return lane, s


@pytest.mark.usefixtures("scoops_on")
async def test_a_half_paid_scoop_is_moved_on_without_a_second_row(monkeypatch):
    lane, s = await _half_paid_scoop(monkeypatch)
    await lane.settle(START + 72 * HOUR_MS)
    got = lane.store.claims[s.id]
    assert got.status == "provisional"
    assert _kinds(await lane.store.claim_ledger(s.id)) == [ledger.PROVISIONAL]
    # It settles from the row it has.
    await lane.settle(got.settle_ms)
    assert _kinds(await lane.store.claim_ledger(s.id)) == [ledger.PROVISIONAL, ledger.SETTLE]


@pytest.mark.usefixtures("scoops_on")
async def test_repays_at_once_pay_a_scoop_once(monkeypatch):
    lane, s = await _half_paid_scoop(monkeypatch)
    lane.store.ledger = [r for r in lane.store.ledger if r.claim_id != s.id]
    row = lane.store.claims[s.id]
    now = START + 72 * HOUR_MS
    await asyncio.gather(*(lane.resolver.repay(lane.store, row, now, HEAD) for _ in range(3)))
    assert lane.store.claims[s.id].status == "provisional"
    assert _kinds(await lane.store.claim_ledger(s.id)) == [ledger.PROVISIONAL]


async def test_a_half_paid_event_claim_is_moved_on_without_a_second_row(monkeypatch):
    lane = Lane(check=True)
    await lane.sources()
    _fail_once(monkeypatch)
    row = await lane.submit("m1", T0, coldkey="ck")
    await lane.step(T0 + 1000)
    await lane.store.append_ledger(LedgerRow(
        hotkey="m1", claim_id=row.id, event_id=None, kind=ledger.PROVISIONAL, points=4.0,
        epoch=1, route="event", category="finance", factors={"base": 10.0, "T": 1.0}))
    await lane.step(T0 + 1000 + resolve.PAY_RETRY_MS)
    assert lane.store.claims[row.id].status == "provisional"
    assert _kinds(await lane.store.claim_ledger(row.id)) == [ledger.PROVISIONAL]


# Closing held scoops

@pytest.mark.usefixtures("scoops_on")
async def test_every_held_scoop_is_closed_past_a_page_and_past_one_that_fails(monkeypatch):
    lane = Lane(check=True)
    await lane.sources()
    held = []
    for i in range(5):
        kw = dict(headline=f"W{i} deal", actors=actors(f"QW{i}1", f"QW{i}2"))
        held.append(await lane.submit(f"m{i}", START + i, coldkey=f"ck{i}",
                                      source_url=f"https://lowblog.net/W{i}", **kw))
    await lane.step(START + 1000)
    assert [lane.store.claims[s.id].status for s in held] == ["verified"] * 5
    monkeypatch.setattr(resolve, "CLOSE_PAGE", 2)
    real = points.debt

    async def broken(st, row, head=None):
        if row.id == held[1].id:
            raise RuntimeError("debt failed")
        return await real(st, row, head)

    monkeypatch.setattr(points, "debt", broken)
    end = START + 72 * HOUR_MS + 10
    await lane.resolver.close_windows(lane.store, end, HEAD)
    status = [lane.store.claims[s.id].status for s in held]
    assert status == ["rejected", "verified", "rejected", "rejected", "rejected"]
    monkeypatch.setattr(points, "debt", real)
    await lane.resolver.close_windows(lane.store, end, HEAD)
    assert all(lane.store.claims[s.id].reason == "scoop_unconfirmed" for s in held)
    assert _kinds(lane.store.ledger).count(ledger.DEBT) == 5
