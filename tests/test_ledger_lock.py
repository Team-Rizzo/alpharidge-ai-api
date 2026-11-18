"""The ledger lock: rows and decisions commit under a shared hold, an epoch is signed under an
exclusive one, so a signed epoch never gains rows and its quality cut is final when signed.
Each test runs on the memory store and, with STORE_TEST_DATABASE_URL set, on Postgres."""

import asyncio
import json
import uuid
from dataclasses import replace

import pytest
from bittensor_wallet import Keypair

from alpharidge_ai.ledger import attestation as ac
from alpharidge_ai.schema import Claim, canonical_body
from app.config import EVENT_LANE
from app.services import intake, ledger, points, quality, recheck
from app.services.store import LedgerRow, MemoryStore, SourceFetchRow
from tests.pipeline import payload
from tests.test_store_contract import T0, claim, store  # noqa: F401 (fixture)

KEY = Keypair.create_from_seed("0x" + "71" * 32)
P = ledger.Params(capacity=10.0, window=3)


async def _fresh_epoch(store) -> int:
    """An epoch after every one signed, so runs on a shared database do not meet."""
    last = await store.last_sealed_epoch(ledger.LEDGER_SIGNER)
    return max(last or 0, 1_000_000) + 1


def _head(epoch: int) -> int:
    return epoch * EVENT_LANE["BLOCK_LENGTH"] + 50


async def _verified(store, hk, ck=None):
    row = await store.create_claim(claim(hk, coldkey=ck))
    assert await store.update_claim(row.id, "received", status="verified")
    return await store.get_claim(row.id)


@pytest.fixture(autouse=True)
def _fresh_quality():
    quality.reset()
    yield
    quality.reset()


async def test_a_pay_open_while_its_epoch_is_signed_is_in_the_signed_window(store, monkeypatch):
    epoch = await _fresh_epoch(store)
    row = await _verified(store, "lock-" + uuid.uuid4().hex[:10])
    entered = asyncio.Event()

    async def slow_schedule(tx, *a, **k):
        # The paid row is written (its epoch chosen); the transaction is still open.
        entered.set()
        await asyncio.sleep(0.3)
        return False

    monkeypatch.setattr(recheck, "schedule", slow_schedule)
    pay = asyncio.ensure_future(points.pay(store, row, "event",
                                           points.Factors(base=12, T=1.0), T0, _head(epoch)))
    await entered.wait()
    signed = await ledger.signed_points(store, epoch, P, KEY)
    await pay
    leaves = await ledger.window_leaves(store, epoch, P.window)
    assert [r.epoch for r in await store.claim_ledger(row.id)] == [epoch]
    assert signed["leaf_count"] == len(leaves)
    assert ac.merkle_root(leaves) == signed["attestation"]["merkleRoot"]


async def test_a_write_after_its_epoch_is_signed_goes_to_the_next(store):
    epoch = await _fresh_epoch(store)
    await ledger.signed_points(store, epoch, P, KEY)
    row = await _verified(store, "lock-" + uuid.uuid4().hex[:10])
    got = await ledger.write(store, LedgerRow(
        hotkey=row.hotkey, claim_id=row.id, event_id=None, kind=ledger.PENALTY, points=-1.0,
        epoch=0, route="event", category="finance"), _head(epoch))
    assert got.epoch == epoch + 1


async def _junk(store, ck):
    row = await store.create_claim(claim("lock-" + uuid.uuid4().hex[:10], coldkey=ck))
    assert await store.update_claim(row.id, "received", status="fetched")
    return await store.get_claim(row.id)


def _hold(monkeypatch, store, claim_id):
    """Holds the claim's update, inside its transaction, until the returned event is set."""
    started, go = asyncio.Event(), asyncio.Event()
    real = type(store).update_claim

    async def held(self, cid, expect, **f):
        if cid == claim_id and f.get("status") == "rejected":
            started.set()
            await go.wait()
        return await real(self, cid, expect, **f)

    monkeypatch.setattr(type(store), "update_claim", held)
    return started, go


async def test_a_decision_open_while_its_epoch_is_signed_is_in_the_cut(store, monkeypatch):
    """A reject stamped before the signing commits before the signing reads, so the epoch's
    outcomes are the same whenever they are read. No settle wait is needed."""
    epoch = await _fresh_epoch(store)
    ck = "lock-ck-" + uuid.uuid4().hex[:8]
    junk = await _junk(store, ck)
    started, go = _hold(monkeypatch, store, junk.id)
    reject = asyncio.ensure_future(intake.transition(store, junk, "rejected",
                                                     reason="judge_reject", judged_ms=T0))
    await started.wait()
    sign = asyncio.ensure_future(ledger.signed_points(store, epoch, P, KEY))
    await asyncio.sleep(0.3)
    assert not sign.done()
    go.set()
    junk = await reject
    await sign
    until = await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch)
    assert junk.decided_ms < until
    first = [x for x in await quality.outcomes(store, 0, until) if x[0] == ck]
    quality.reset()
    assert first == [x for x in await quality.outcomes(store, 0, until) if x[0] == ck] == [
        (ck, False, junk.id)]


def _slow_window(monkeypatch):
    """Holds the signing's window read until the returned event is set."""
    inside, go = asyncio.Event(), asyncio.Event()
    real = ledger.window_leaves

    async def slow(st, e, size=None):
        inside.set()
        await go.wait()
        return await real(st, e, size)

    monkeypatch.setattr(ledger, "window_leaves", slow)
    return inside, go


async def test_writes_and_decisions_go_on_while_an_epoch_is_signed(store, monkeypatch):
    """The exclusive hold only seals the epoch. While its window is read and signed, a
    decision and a ledger row commit at once; the decision is after the epoch's cut and the
    row lands in the next epoch."""
    epoch = await _fresh_epoch(store)
    ck = "lock-ck-" + uuid.uuid4().hex[:8]
    junk = await _junk(store, ck)
    inside, go = _slow_window(monkeypatch)
    sign = asyncio.ensure_future(ledger.signed_points(store, epoch, P, KEY))
    await inside.wait()
    assert await store.sealed_ms(ledger.LEDGER_SIGNER, epoch) is not None
    assert await store.get_attestation(ledger.LEDGER_SIGNER, epoch) is None
    junk = await asyncio.wait_for(intake.transition(store, junk, "rejected",
                                                    reason="judge_reject", judged_ms=T0), 5)
    late_hk = "late-" + uuid.uuid4().hex[:8]
    late = await asyncio.wait_for(ledger.write(store, LedgerRow(
        hotkey=late_hk, claim_id=None, event_id=None, kind=ledger.PENALTY, points=-1.0,
        epoch=0, route="event", category="finance"), _head(epoch)), 5)
    assert late.epoch == epoch + 1 and not sign.done()
    go.set()
    signed = await sign
    until = await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch)
    assert until == await store.sealed_ms(ledger.LEDGER_SIGNER, epoch)
    assert junk.decided_ms >= until
    assert [x for x in await quality.outcomes(store, 0, until) if x[0] == ck] == []
    leaves = await ledger.window_leaves(store, epoch, P.window)
    assert ac.merkle_root(leaves) == signed["attestation"]["merkleRoot"]
    assert late_hk not in {lf["miner_hotkey"] for lf in leaves}


async def test_a_signing_that_fails_after_the_seal_is_done_again_from_it(store, monkeypatch):
    epoch = await _fresh_epoch(store)
    row = await _verified(store, "lock-" + uuid.uuid4().hex[:10])
    await points.pay(store, row, "event", points.Factors(base=12, T=1.0), T0, _head(epoch))
    real, calls = ledger._attest, []

    def broken(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("signer down")
        return real(*a, **k)

    monkeypatch.setattr(ledger, "_attest", broken)
    with pytest.raises(RuntimeError):
        await ledger.signed_points(store, epoch, P, KEY)
    cut = await store.sealed_ms(ledger.LEDGER_SIGNER, epoch)
    assert cut is not None and await store.get_attestation(ledger.LEDGER_SIGNER, epoch) is None
    assert await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch) is None
    assert (await store.last_attestation_epoch(ledger.LEDGER_SIGNER) or 0) < epoch
    rows = [r.id for r in await store.ledger_rows([epoch])]
    late = await ledger.write(store, LedgerRow(
        hotkey="late", claim_id=None, event_id=None, kind=ledger.PENALTY, points=-1.0,
        epoch=0, route="event", category="finance"), _head(epoch))
    assert late.epoch == epoch + 1
    # A request for the next epoch signs this one first, from its seal.
    nxt = await ledger.signed_points(store, epoch + 1, P, KEY)
    assert await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch) == cut
    assert [r.id for r in await store.ledger_rows([epoch])] == rows
    signed = await ledger.signed_points(store, epoch, P, KEY)
    assert ac.merkle_root(await ledger.window_leaves(store, epoch, P.window)) == (
        signed["attestation"]["merkleRoot"])
    assert nxt["leaf_count"] == len(await ledger.window_leaves(store, epoch + 1, P.window))
    assert await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch + 1) > cut


async def test_a_pay_is_in_the_cut_of_the_epoch_its_row_is_in(store):
    """Before the seal: row in the epoch, decision in its cut. After: row in the next
    epoch, decision after this cut and inside the next one's."""
    epoch = await _fresh_epoch(store)
    hk = "lock-" + uuid.uuid4().hex[:10]
    ck = "lock-ck-" + uuid.uuid4().hex[:8]
    f = points.Factors(base=12, T=1.0)
    early = await points.pay(store, await _verified(store, hk, ck), "event", f, T0,
                             _head(epoch))
    await ledger.signed_points(store, epoch, P, KEY)
    late = await points.pay(store, await _verified(store, hk, ck), "event", f, T0, _head(epoch))
    await ledger.signed_points(store, epoch + 1, P, KEY)
    cut = await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch)
    nxt = await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch + 1)
    assert [r.epoch for r in await store.claim_ledger(early.id)] == [epoch]
    assert [r.epoch for r in await store.claim_ledger(late.id)] == [epoch + 1]
    assert early.decided_ms < cut <= late.decided_ms < nxt
    assert [c for o, _, c in await quality.outcomes(store, 0, cut) if o == ck] == [early.id]
    assert [c for o, _, c in await quality.outcomes(store, 0, nxt) if o == ck] == [
        early.id, late.id]


async def test_the_cut_holds_within_one_millisecond(monkeypatch):
    """A decision stamped in the millisecond its epoch is sealed is in the cut when it
    committed first, and after it otherwise: the cut is one past the sealing clock and a
    later stamp is never below the last cut."""
    store = MemoryStore()
    now = T0 + 5
    monkeypatch.setattr(intake, "_now_ms", lambda: now)
    before, after = await _junk(store, "ck"), await _junk(store, "ck")
    before = await intake.transition(store, before, "rejected", reason="judge_reject",
                                     judged_ms=T0)
    await ledger.signed_points(store, 7, P, KEY)
    after = await intake.transition(store, after, "rejected", reason="judge_reject",
                                    judged_ms=T0)
    cut = await store.attestation_issued_ms(ledger.LEDGER_SIGNER, 7)
    assert (before.decided_ms, cut, after.decided_ms) == (now, now + 1, now + 1)
    assert [c for _, _, c in await quality.outcomes(store, 0, cut)] == [before.id]
    await ledger.signed_points(store, 8, P, KEY)
    assert await store.attestation_issued_ms(ledger.LEDGER_SIGNER, 8) == now + 2


async def test_a_wait_for_the_ledger_lock_gives_up_before_the_transaction_limit(store,
                                                                                monkeypatch):
    from app import db
    monkeypatch.setattr(db, "LEDGER_LOCK_TIMEOUT_MS", 300)
    junk = await _junk(store, "lock-ck-" + uuid.uuid4().hex[:8])
    held, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with store.transaction() as tx:
            await tx.lock_ledger(exclusive=True)
            held.set()
            await release.wait()

    holder = asyncio.ensure_future(hold())
    await held.wait()
    t = asyncio.get_running_loop().time()
    with pytest.raises(Exception):
        await intake.transition(store, junk, "rejected", reason="judge_reject")
    assert asyncio.get_running_loop().time() - t < 5
    release.set()
    await holder
    assert (await store.get_claim(junk.id)).status == "fetched"
    assert (await intake.transition(store, junk, "rejected", reason="judge_reject")).status == (
        "rejected")


async def test_every_ledger_row_is_written_under_the_lock(monkeypatch):
    """Pay, settle, clawback, penalty, debt and carry rows all take the shared (or, for the
    carry, the signing's exclusive) hold."""
    store = MemoryStore()
    seen = []
    real = store.append_ledger

    async def checked(row):
        seen.append((row.kind, store.ledger_lock_held()))
        return await real(row)

    store.append_ledger = checked
    head = _head(10)
    paid = await _verified(store, "hk", "ck")
    f = points.Factors(base=12, T=1.0)
    paid = await points.pay(store, paid, "event", f, T0, head)
    await points.settle_one(store, paid, head)
    await ledger.clawback(store, [await store.get_claim(paid.id)], "clawed:test", head)
    await ledger.penalty(store, await store.get_claim(paid.id), "penalty:test", head)
    body = canonical_body(Claim(**payload()))
    scoop = await store.create_claim(replace(claim("hk2", coldkey="ck2"), body=body.decode(),
                                             payload=json.loads(body)))
    await store.update_claim(scoop.id, "received", status="rejected",
                             reason="scoop_unconfirmed", route="scoop")
    await store.add_source_fetch(SourceFetchRow(claim_id=scoop.id, verdict="verified",
                                                published_ms=T0))
    await points.debt(store, await store.get_claim(scoop.id), head)
    # Epoch 10's rows leave the window at 13: its debt carries.
    for e in range(10, 14):
        await ledger.signed_points(store, e, P, KEY)
    kinds = {k for k, _ in seen}
    assert {"provisional", "settle", "clawback", "penalty", "debt", "carry"} <= kinds
    assert all(held == ("exclusive" if k == "carry" else "shared") for k, held in seen), seen


async def test_a_decision_outside_a_transaction_takes_the_lock_too():
    store = MemoryStore()
    row = await _junk(store, "ck")
    held = []
    real = store.update_claim

    async def checked(cid, expect, **f):
        held.append(store.ledger_lock_held())
        return await real(cid, expect, **f)

    store.update_claim = checked
    await intake.transition(store, row, "rejected", reason="judge_reject")
    assert held == ["shared"]
    with pytest.raises(intake.InvalidTransition):
        await intake.transition(store, row, "rejected", reason="judge_reject")
    assert store.ledger_lock_held() is None


def _row(epoch, hk, pts, kind=ledger.PENALTY):
    return LedgerRow(hotkey=hk, claim_id=None, event_id=None, kind=kind, points=pts, epoch=epoch,
                     route=ledger.CARRY if kind == ledger.CARRY else "event", category="corp",
                     received_ms=0 if kind == ledger.CARRY else T0, bucket=0, coldkey="ck-" + hk)


async def _carry(store, epoch, hk):
    return [(r.hotkey, r.points) for r in await store.ledger_rows([epoch])
            if r.kind == ledger.CARRY and r.hotkey == hk]


@pytest.mark.parametrize("left", [[-4.0], [-4.0, -1.5], [-1.5]])
async def test_carry_rows_left_by_a_stopped_signing_are_replaced_by_the_seal(store, left):
    """The code before the seal wrote an epoch's carry rows, then its attestation. A writer
    stopped between the two left carry rows, some or all, in an unsigned epoch; the seal
    replaces them, so the epoch has one carry set."""
    epoch = await _fresh_epoch(store) + 5
    hk = "carry-" + uuid.uuid4().hex[:10]
    for e in range(epoch - 4, epoch):
        await store.append_ledger(_row(e, hk, -6.0 if e == epoch - 3 else 1.0))
    await ledger.signed_points(store, epoch - 1, P, KEY)
    for pts in left:
        await store.append_ledger(_row(epoch, hk, pts, ledger.CARRY))
    signed = await ledger.signed_points(store, epoch, P, KEY)
    assert await _carry(store, epoch, hk) == [(hk, -4.0)]
    leaves = await ledger.window_leaves(store, epoch, P.window)
    assert signed["leaf_count"] == len(leaves)
    assert ac.merkle_root(leaves) == signed["attestation"]["merkleRoot"]


async def test_the_carry_of_a_sealed_epoch_is_never_dropped(store):
    epoch = await _fresh_epoch(store)
    hk = "carry-" + uuid.uuid4().hex[:10]
    await ledger.signed_points(store, epoch, P, KEY)
    await store.append_ledger(_row(epoch, hk, -2.0, ledger.CARRY))
    with pytest.raises(Exception):
        async with store.transaction() as tx:
            await tx.lock_ledger(exclusive=True)
            await tx.drop_carry(ledger.LEDGER_SIGNER, epoch)
    assert await _carry(store, epoch, hk) == [(hk, -2.0)]


async def test_ledger_rows_stay_append_only_in_postgres(store):
    """Only the seal's drop of an unsealed epoch's carry may delete; nothing may update."""
    if isinstance(store, MemoryStore):
        pytest.skip("the database's trigger")
    signed = await _fresh_epoch(store)
    await ledger.signed_points(store, signed, P, KEY)
    epoch, hk = signed + 1, "carry-" + uuid.uuid4().hex[:10]
    carry = await store.append_ledger(_row(epoch, hk, -2.0, ledger.CARRY))
    other = await store.append_ledger(_row(epoch, hk, 1.0))
    kept = await store.append_ledger(_row(signed, hk, -3.0, ledger.CARRY))
    for sql in ('DELETE FROM ledger_entry WHERE id = $1',
                'UPDATE ledger_entry SET points = 0 WHERE id = $1'):
        for row in (carry, other):
            with pytest.raises(Exception, match="append-only"):
                await store.db.execute_raw(sql, row.id)
    for row in (other, kept):
        with pytest.raises(Exception, match="append-only"):
            async with store.transaction() as tx:
                await tx.db.execute_raw("SET LOCAL ledger.replace_carry = 'on'")
                await tx.db.execute_raw('DELETE FROM ledger_entry WHERE id = $1', row.id)
    async with store.transaction() as tx:
        await tx.lock_ledger(exclusive=True)
        assert await tx.drop_carry(ledger.LEDGER_SIGNER, epoch) == 1
    assert [r.id for r in await store.ledger_rows([epoch]) if r.hotkey == hk] == [other.id]
    assert [r.id for r in await store.ledger_rows([signed]) if r.hotkey == hk] == [kept.id]


async def test_clear_seals_deletes_only_unsigned_seals(store, capsys):
    """The rollback script. After it the epoch is signed again from a new seal, its carry rows
    replaced rather than doubled."""
    if isinstance(store, MemoryStore):
        pytest.skip("runs SQL")
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "clear_seals", Path(__file__).parent.parent / "scripts" / "clear_seals.py")
    clear_seals = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(clear_seals)
    epoch = await _fresh_epoch(store) + 4
    hk = "carry-" + uuid.uuid4().hex[:10]
    for e in range(epoch - 4, epoch):
        await store.append_ledger(_row(e, hk, -6.0 if e == epoch - 3 else 1.0))
    signed = await ledger.signed_points(store, epoch - 1, P, KEY)
    await ledger.seal(store, epoch, P.window)
    assert await _carry(store, epoch, hk) == [(hk, -4.0)]
    await clear_seals.run(store.db, apply=False)
    assert await store.sealed_ms(ledger.LEDGER_SIGNER, epoch) is not None
    await clear_seals.run(store.db, apply=True)
    assert f"seal: epoch {epoch} " in capsys.readouterr().out
    assert await store.sealed_ms(ledger.LEDGER_SIGNER, epoch) is None
    assert (await ledger.signed_points(store, epoch - 1, P, KEY))["signature"] == (
        signed["signature"])
    again = await ledger.signed_points(store, epoch, P, KEY)
    assert await _carry(store, epoch, hk) == [(hk, -4.0)]
    assert again["leaf_count"] == len(await ledger.window_leaves(store, epoch, P.window))


async def test_a_wait_for_a_claim_row_gives_up_like_the_ledger_lock(store, monkeypatch):
    """The ledger lock's lock_timeout holds for the rest of its transaction, so a wait for a
    claim's row lock fails too instead of keeping its pooled connection."""
    if isinstance(store, MemoryStore):
        pytest.skip("Postgres row locks")
    from app import db
    monkeypatch.setattr(db, "LEDGER_LOCK_TIMEOUT_MS", 300)
    row = await _verified(store, "lock-" + uuid.uuid4().hex[:10])
    held, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with store.transaction() as tx:
            await tx.lock_ledger()
            await tx.lock_claim(row.id)
            held.set()
            await release.wait()

    holder = asyncio.ensure_future(hold())
    await held.wait()
    t = asyncio.get_running_loop().time()
    for wait in (lambda tx: tx.lock_claim(row.id),
                 lambda tx: tx.update_claim(row.id, "verified", status="rejected")):
        with pytest.raises(Exception, match="lock timeout"):
            async with store.transaction() as tx:
                await tx.lock_ledger()
                await wait(tx)
    assert asyncio.get_running_loop().time() - t < 5
    release.set()
    await holder
    assert (await store.get_claim(row.id)).status == "verified"
