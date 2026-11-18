"""A pay whose transaction fails is paid later: its transaction waits for a pooled connection
as long as a plain query does, and the resolver pays a verified prize claim left unpaid."""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

from app.services import ledger, points, resolve, scoops
from tests.pipeline import HEAD, HOUR_MS, T0, Lane
from tests.test_guards import START, actors, deal_page, found
from tests.test_store_contract import claim


def _fail_once(monkeypatch):
    real, calls = points.pay, []

    async def pay(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transaction failed to start")
        return await real(*a, **k)

    monkeypatch.setattr(points, "pay", pay)
    return calls


async def test_a_verified_event_claim_whose_pay_failed_is_paid_later(monkeypatch):
    lane = Lane(check=True)
    await lane.sources()
    calls = _fail_once(monkeypatch)
    row = await lane.submit("m1", T0, coldkey="ck")
    await lane.step(T0 + 1000)
    got = lane.store.claims[row.id]
    assert (got.status, got.route, calls) == ("verified", "event", [1])
    assert await lane.store.claim_ledger(row.id) == []
    # Not before the retry wait: a pay may still be on its way.
    await lane.step(T0 + 1000 + resolve.PAY_RETRY_MS - 1)
    assert lane.store.claims[row.id].status == "verified"
    await lane.step(T0 + 1000 + resolve.PAY_RETRY_MS)
    got = lane.store.claims[row.id]
    (paid,) = await lane.store.claim_ledger(row.id)
    assert (got.status, paid.kind, paid.route) == ("provisional", ledger.PROVISIONAL, "event")

    # Paid as the first try would have paid it.
    other = Lane(check=True)
    await other.sources()
    again = await other.submit("m1", T0, coldkey="ck")
    await other.step(T0 + 1000)
    (want,) = await other.store.claim_ledger(again.id)
    assert (paid.points, paid.factors) == (want.points, want.factors)
    await lane.step(T0 + 2 * resolve.PAY_RETRY_MS)
    assert len(await lane.store.claim_ledger(row.id)) == 1


@pytest.mark.usefixtures("scoops_on")
async def test_a_confirmed_scoop_whose_pay_failed_is_paid_not_closed_with_debt(monkeypatch):
    lane = Lane(check=True)
    await lane.sources()
    kw = dict(headline="R deal", actors=actors("QR1", "QR2"))
    s = await lane.submit("m4", START, source_url="https://lowblog.net/R", **kw)
    await lane.step(START + 1000)
    assert lane.store.claims[s.id].status == "verified"
    _fail_once(monkeypatch)
    with pytest.raises(RuntimeError):
        await found(lane, s, "https://wiretwo.org/R", START - 1000, START + 70_000,
                    deal_page("Company QR1", "Company QR2"))
    assert lane.store.claims[s.id].status == "verified"
    assert ledger.judge_record(await lane.store.source_fetches(s.id))["confirm"]
    # Its found page is given up on; at the window's end it is paid as confirmed.
    for cand in await lane.store.scoop_candidates(s.id, (scoops.QUEUED,)):
        await lane.store.update_scoop_candidate(cand.id, status=scoops.REJECTED,
                                                reason="check_failed")
    await lane.settle(START + 72 * HOUR_MS)
    got = lane.store.claims[s.id]
    kinds = [r.kind for r in await lane.store.claim_ledger(s.id)]
    assert (got.status, got.route, kinds) == ("provisional", "scoop", [ledger.PROVISIONAL])


async def test_a_pay_waits_for_a_pooled_connection_as_a_query_does():
    """Both pooled connections are busy for longer than Prisma's 2 s transaction start
    default; the pay's transaction waits for one instead of failing."""
    url = os.getenv("STORE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("STORE_TEST_DATABASE_URL not set")
    from prisma import Prisma

    from app.db import pooled_url
    from app.services.store.prisma import PrismaStore
    client = Prisma(datasource={"url": pooled_url(url, "2", "20")})
    await client.connect()
    try:
        store = PrismaStore(client)
        row = await store.create_claim(claim("pool-" + uuid.uuid4().hex[:10]))
        assert await store.update_claim(row.id, "received", status="verified")
        row = await store.get_claim(row.id)
        busy = []

        async def hold():
            async with client.tx(timeout=timedelta(seconds=15)) as tx:
                await tx.query_raw("SELECT pg_sleep(3) IS NULL AS x")

        real = store.source_fetches

        async def then_busy(claim_id):
            # The pay's last read before its transaction; then the pool fills up.
            out = await real(claim_id)
            busy.extend(asyncio.ensure_future(hold()) for _ in range(2))
            await asyncio.sleep(0.5)
            return out

        store.source_fetches = then_busy
        got = await points.pay(store, row, "event", points.Factors(base=12, T=1.0), T0, HEAD)
        await asyncio.gather(*busy)
        assert got.status == "provisional"
        assert [r.kind for r in await store.claim_ledger(row.id)] == [ledger.PROVISIONAL]
    finally:
        await client.disconnect()
