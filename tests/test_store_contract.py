"""The memory store used by the tests and the Prisma store give the same results. The Prisma
side runs with STORE_TEST_DATABASE_URL set to a scratch database (the local e2e run does)."""

import os
import uuid
from dataclasses import replace

import pytest

from app.services.store import MemoryStore
from app.services.store.rows import (
    ClaimRow, EventRecord, JudgeItemRow, JudgePostRow, ReverifyRow, ScoopCandidateRow, SourceFetchRow,
)

T0 = 1_790_000_000_000


@pytest.fixture(params=["memory", "prisma"])
async def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    url = os.getenv("STORE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("STORE_TEST_DATABASE_URL not set")
    from prisma import Prisma

    from app.services.store.prisma import PrismaStore
    client = Prisma(datasource={"url": url})
    await client.connect()
    try:
        yield PrismaStore(client)
    finally:
        await client.disconnect()


def claim(hotkey: str, **kw) -> ClaimRow:
    payload = {"kind": "event", "event_type": "merger_acquisition", "headline": "Acme buys Beta"}
    return ClaimRow(hotkey=hotkey, signature="s", nonce=uuid.uuid4().hex, signed_at=T0 // 1000,
                    body_hash=uuid.uuid4().hex, body="{}", received_ms=T0, bucket=0,
                    kind="event", category="finance", event_type="merger_acquisition",
                    payload=payload, source_url="https://a.example/x", **kw)


async def test_claim_update_retype_and_read(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk), jobs=("novelty",))
    got = await store.get_claim(row.id)
    assert (got.hotkey, got.status, got.payload["headline"]) == (hk, "received", "Acme buys Beta")
    assert await store.claim_by_body(hk, row.body_hash) == got

    assert not await store.update_claim(row.id, "novelty", status="verified")
    assert await store.update_claim(row.id, "received", status="novelty", novelty="new")
    retyped = {**got.payload, "event_type": "funding"}
    assert await store.update_claim(row.id, "novelty", status="provisional", event_type="funding",
                                    payload=retyped, verified_ms=T0 + 5, trust=0.7,
                                    trust_band="prize-eligible")
    got = await store.get_claim(row.id)
    assert (got.status, got.novelty, got.event_type, got.payload["event_type"]) == (
        "provisional", "new", "funding", "funding")
    assert (got.verified_ms, got.trust, got.trust_band) == (T0 + 5, 0.7, "prize-eligible")


async def test_fetch_check_and_judge_record_read_back(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk))
    rec = await store.add_source_fetch(SourceFetchRow(
        claim_id=row.id, verdict="pending", http_status=200, content_hash="h1",
        fetched_ms=T0, check={"page": {"bylines": 1}}))
    await store.update_source_fetch(rec.id, verdict="verified", reason=None, spans=[[0, 5]],
                                    check={**rec.check, "judge": {"outcome": "pass"}})
    (got,) = await store.source_fetches(row.id)
    assert (got.verdict, got.spans, got.check) == (
        "verified", [[0, 5]], {"page": {"bylines": 1}, "judge": {"outcome": "pass"}})

    key = uuid.uuid4().hex
    assert await store.judge_verdict(key) is None
    await store.add_judge_verdict(key, row.id, {"outcome": "pass", "answers": {"event": "yes"}})
    assert await store.judge_verdict(key) == {"outcome": "pass", "answers": {"event": "yes"}}


async def test_scoop_candidates_are_once_per_url(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk))
    first = await store.add_scoop_candidate(ScoopCandidateRow(row.id, "https://b.example/1",
                                                              "v1", T0))
    assert first is not None
    assert await store.add_scoop_candidate(ScoopCandidateRow(row.id, "https://b.example/1",
                                                             "v2", T0 + 1)) is None
    await store.update_scoop_candidate(first.id, status="confirmed")
    (got,) = await store.scoop_candidates(row.id)
    assert (got.validator, got.found_ms, got.status) == ("v1", T0, "confirmed")
    assert await store.scoop_candidates(row.id, ("queued",)) == []


async def test_judge_items_once_per_key_and_posts_once_per_validator(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk))
    key = uuid.uuid4().hex
    task = {"kind": "claim", "url": "https://a.example/x", "at": "2026-10-13T12:00:00Z"}
    item = await store.add_judge_item(JudgeItemRow(key, row.id, "claim", task, T0, hotkey=hk))
    again = await store.add_judge_item(JudgeItemRow(key, row.id, "claim", {}, T0 + 5))
    assert again == item and (item.task, item.created_ms, item.status) == (task, T0, "pending")
    assert await store.judge_items_count(hk, T0) == 1
    assert await store.judge_items_count(hk, T0 + 1) == 0
    assert item.id in [i.id for i in await store.judge_items("pending", limit=100_000)]

    body = '{"key":"%s"}' % key
    post = JudgePostRow(item.id, "v1", body, 1700000000, "n1", "ab", T0 + 10)
    first = await store.add_judge_post(post)
    assert first is not None and await store.add_judge_post(post) is None
    await store.add_judge_post(JudgePostRow(item.id, "v2", body, 1700000001, "n2", "cd", T0 + 11))
    got = await store.judge_posts([item.id])
    assert [(p.validator, p.body, p.signed_at, p.posted_ms) for p in got] == [
        ("v1", body, 1700000000, T0 + 10), ("v2", body, 1700000001, T0 + 11)]
    await store.update_judge_item(item.id, "done")
    assert (await store.judge_item(key)).status == "done"


async def test_dashboard_validator_reads(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    v = "val-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk))
    item = await store.add_judge_item(JudgeItemRow(uuid.uuid4().hex, row.id, "claim", {}, T0))
    await store.add_judge_post(JudgePostRow(item.id, v, "{}", 1, "n", "s", T0 + 10))
    assert [i.id for i in await store.judge_items_for_claims([row.id])] == [item.id]
    assert [(c, p.item_id, p.posted_ms) for c, p in await store.judge_posts_since(T0)
            if p.validator == v] == [(row.id, item.id, T0 + 10)]
    assert not [p for _, p in await store.judge_posts_since(T0 + 11) if p.validator == v]
    await store.add_scoop_candidate(ScoopCandidateRow(row.id, "https://b.example/2", v, T0))
    assert [c.validator for c in await store.candidates_for_scoops([row.id])] == [v]
    epoch = 900_000_000 + int(uuid.uuid4().int % 1_000_000)
    await store.put_reverify(ReverifyRow(row.id, v, epoch, "agree"))
    await store.add_reverify_report(v, epoch, T0 + 20)
    assert (await store.reverify_reports_since(T0 + 20)).get((v, epoch)) == T0 + 20
    assert (v, epoch) not in await store.reverify_reports_since(T0 + 21)
    assert [(r.claim_id, r.verdict) for r in await store.reverify_in_epochs([epoch])] == [
        (row.id, "agree")]
    assert [c.id for c in await store.claims_by_ids([row.id, row.id])] == [row.id]


async def test_scoop_record_is_per_coldkey(store):
    ck, hk = "contract-" + uuid.uuid4().hex[:12], "contract-" + uuid.uuid4().hex[:12]
    for i, (key, cold, status, reason) in enumerate((
            (hk, ck, "settled", None), (hk + "b", ck, "rejected", "scoop_unconfirmed"),
            (hk, ck, "rejected", "known_event"), (ck, None, "rejected", "scoop_mismatch"))):
        row = await store.create_claim(claim(key, coldkey=cold))
        assert await store.update_claim(row.id, "received", status=status, reason=reason,
                                        route="scoop")
    assert await store.scoop_record(T0 - 1, [ck, hk]) == {ck: (1, 3)}


async def test_open_scoops_and_the_first_trusted_page_on_an_event(store):
    from datetime import date
    ck = "contract-" + uuid.uuid4().hex[:12]
    ev = await store.add_event(EventRecord("merger_acquisition", "finance", date(2026, 10, 13),
                                           "fp-" + uuid.uuid4().hex))
    rows = []
    for i, (cold, status, route, t, reason) in enumerate((
            (ck, "verified", "scoop", 0.1, None), (ck, "verified", "scoop", 0.1, None),
            (ck, "provisional", "scoop", 0.1, None), (None, "rejected", "event", 0.9,
                                                       "known_event"),
            (None, "rejected", "event", 0.9, "judge_reject"), (None, "fetched", "event", 0.5,
                                                               None))):
        row = await store.create_claim(replace(claim(ck + str(i), coldkey=cold),
                                               received_ms=T0 + i * 1000))
        assert await store.update_claim(row.id, "received", status=status, route=route,
                                        trust=t, reason=reason, event_id=ev.id)
        rows.append(row)
    assert await store.open_scoops([ck, "nobody"]) == {ck: 2}
    await store.add_source_fetch(SourceFetchRow(claim_id=rows[3].id, verdict="supported",
                                                published_ms=T0 - 5000))
    await store.add_source_fetch(SourceFetchRow(claim_id=rows[4].id, verdict="rejected",
                                                published_ms=T0 - 9000))
    assert await store.first_trusted_ms(ev.id, rows[0].id, 0.75) == T0 - 5000
    assert await store.first_trusted_ms(ev.id, rows[3].id, 0.75) is None
    assert await store.first_trusted_ms(ev.id, rows[0].id, 0.75, ("judge_reject",)) == T0 - 9000


async def test_claims_page_filters_by_type_and_reason(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    a = await store.create_claim(claim(hk))
    b = await store.create_claim(claim(hk))
    assert await store.update_claim(a.id, "received", status="rejected", reason="known_event")
    assert await store.update_claim(b.id, "received", status="rejected", reason="known_event",
                                    event_type="funding")
    got = await store.claims_page(T0 + 1, hotkey=hk, event_type="funding",
                                  reason="known_event")
    assert [c.id for c in got] == [b.id]
    assert await store.claims_page(T0 + 1, hotkey=hk, reason="judge_reject") == []


async def test_a_retried_job_keeps_its_attempts(store):
    row = await store.create_claim(claim("contract-" + uuid.uuid4().hex[:12]))
    job = await store.add_job("contract_retry", row.id, T0)
    (got,) = await store.take_jobs(["contract_retry"], 10, 60_000, T0)
    await store.defer_job(got.id, T0 + 1, retry=True)
    (again,) = await store.take_jobs(["contract_retry"], 10, 60_000, T0 + 2)
    assert (again.id, again.attempts) == (job.id, 2)
    await store.finish_job(job.id)


async def test_concurrent_receipts_link_in_one_sequence(store):
    import asyncio

    from alpharidge_ai.ledger import receipts as rc
    from app.services import receipts

    hk = "contract-" + uuid.uuid4().hex[:12]
    rows = [replace(claim(hk), received_ms=T0 + i) for i in range(12)]
    made = await asyncio.gather(*[store.create_claim(r, receipt=receipts.builder(r))
                                  for r in rows])
    got = await store.receipts_for([m.id for m in made])
    seqs = sorted(r["seq"] for r in got.values())
    assert seqs == list(range(seqs[0], seqs[0] + 12))
    by_seq = sorted(got.values(), key=lambda r: r["seq"])
    assert all(b["prev"] == a["hash"] for a, b in zip(by_seq, by_seq[1:]))
    assert all(rc.valid(r, rc_pubkey()) for r in by_seq)
    assert await store.receipt_of(made[0].id) == got[made[0].id]


def rc_pubkey():
    from bittensor_wallet import Keypair
    return Keypair.create_from_seed("0x" + os.environ["API_ATTESTATION_PRIVKEY"]).ss58_address


async def test_prize_docs_probe_keeps_every_document_that_matches(store):
    import random

    from app.services import novelty
    rng = random.Random(7)

    def hexes(n):
        return "".join(rng.choice("0123456789abcdef") for _ in range(n))

    base = hexes(16) + hexes(16) + hexes(64 * 8)
    docs = [base[:16] + hexes(len(base) - 16), hexes(16) + base[16:32] + hexes(64 * 8),
            hexes(16) + "0" * 16 + base[32:], base[:40]]
    # The least overlap that still matches, all in the last slots.
    keeps = [set(range(25, 64))]
    keeps += [set(rng.sample(range(64), n)) for n in (37, 38, 39, 40, 64) for _ in range(4)]
    for keep in keeps:
        sig = "".join(base[32 + i * 8:40 + i * 8] if i in keep else hexes(8) for i in range(64))
        docs.append(hexes(32) + sig)
    hk = "contract-" + uuid.uuid4().hex[:12]
    for d in docs:
        row = await store.create_claim(claim(hk))
        assert await store.update_claim(row.id, "received", status="provisional", route="event",
                                        doc_hash=d)
    for doc in (base, hexes(16) + "0" * 16 + base[32:], docs[4], docs[9]):
        probed = await store.prize_docs(T0 - 1, novelty.doc_probe(doc))
        every = await store.prize_docs(T0 - 1)
        assert (sum(novelty.same_doc(doc, d) for d in probed)
                == sum(novelty.same_doc(doc, d) for d in every))
        assert sum(novelty.same_doc(doc, d) for d in every) > 0


async def test_won_by_host_counts_paid_claims_per_host_and_type(store):
    host = f"h{uuid.uuid4().hex[:8]}.example.org"
    hk = "contract-" + uuid.uuid4().hex[:12]
    for i, (status, at) in enumerate((("provisional", T0 + 10), ("provisional", T0 - 7_000_000),
                                      ("rejected", T0 + 10), ("settled", T0 + 20))):
        row = await store.create_claim(replace(claim(hk), source_url=f"https://{host}/p{i}"))
        assert await store.update_claim(row.id, "received", status=status, route="event",
                                        verified_ms=at)
    got = [r for r in await store.won_by_host(T0 - 86_400_000, T0 - 3_600_000)
           if r[0] == host]
    assert got == [(host, "merger_acquisition", 2, 3)]


async def test_quality_outcomes_are_passes_and_junk_in_decided_order(store):
    ck, hk = "contract-ck-" + uuid.uuid4().hex[:8], "contract-" + uuid.uuid4().hex[:12]
    done = [("provisional", None, 3), ("rejected", "judge_reject", 1), ("rejected", "known_event", 2),
            ("settled", None, 4), ("rejected", "not_an_event", 0)]
    for status, reason, at in done:
        row = await store.create_claim(claim(hk, coldkey=ck))
        assert await store.update_claim(row.id, "received", status=status, reason=reason,
                                        judged_ms=T0 + at, decided_ms=T0 + at)
    await store.create_claim(claim(hk, coldkey=ck))
    got = [x[:2] for x in await store.quality_outcomes(T0 + 1, ("judge_reject", "not_an_event"))
           if x[0] == ck]
    assert got == [(ck, False), (ck, True), (ck, True)]
    got = [x[:2] for x in await store.quality_outcomes(T0 + 1, ("judge_reject", "not_an_event"),
                                                       T0 + 4) if x[0] == ck]
    assert got == [(ck, False), (ck, True)]


async def test_quality_keeps_a_clawed_claim_and_counts_a_typeless_unclear_as_junk(store):
    ck, hk = "contract-ck-" + uuid.uuid4().hex[:8], "contract-" + uuid.uuid4().hex[:12]
    ids = []
    for status, reason, at in [("clawed_back", None, 1), ("rejected", "judge_unclear", 2),
                               ("rejected", "judge_unclear", 3)]:
        row = await store.create_claim(claim(hk, coldkey=ck))
        assert await store.update_claim(row.id, "received", status=status, reason=reason,
                                        judged_ms=T0 + at, decided_ms=T0 + at)
        ids.append(row.id)
    await store.add_source_fetch(SourceFetchRow(claim_id=ids[1], verdict="rejected",
                                                check={"judge": {"type_none": True}}))
    await store.add_source_fetch(SourceFetchRow(claim_id=ids[2], verdict="rejected",
                                                check={"judge": {"outcome": "unclear"}}))
    got = [x for x in await store.quality_outcomes(T0, ("judge_reject",)) if x[0] == ck]
    assert got == [(ck, True, ids[0]), (ck, False, ids[1])]


async def test_an_attestation_keeps_its_issue_time(store):
    signer, epoch = "contract-signer-" + uuid.uuid4().hex[:8], 7
    assert await store.attestation_issued_ms(signer, epoch) is None
    body = {"perMinerPoints": {}, "totalPoints": 0.0, "merkleRoot": "00", "signature": "00",
            "capacity": 1.0, "leafCount": 0}
    await store.put_attestation(signer, epoch, body)
    first = await store.attestation_issued_ms(signer, epoch)
    await store.put_attestation(signer, epoch, body)
    assert first is not None and await store.attestation_issued_ms(signer, epoch) == first


async def test_quality_outcomes_go_by_when_each_claim_was_decided(store):
    ck, hk = "contract-ck-" + uuid.uuid4().hex[:8], "contract-" + uuid.uuid4().hex[:12]
    # (status, reason, judged, decided): judged order is not decision order.
    done = [("provisional", None, 0, 5), ("rejected", "judge_reject", 1, 2),
            ("settled", None, 2, 3), ("verified", None, 3, None),
            ("rejected", "judge_reject", 4, None)]
    ids = []
    for status, reason, judged, decided in done:
        row = await store.create_claim(claim(hk, coldkey=ck))
        assert await store.update_claim(
            row.id, "received", status=status, reason=reason, judged_ms=T0 + judged,
            decided_ms=None if decided is None else T0 + decided)
        ids.append(row.id)
    got = [x for x in await store.quality_outcomes(T0, ("judge_reject",)) if x[0] == ck]
    assert got == [(ck, False, ids[1]), (ck, True, ids[2]), (ck, True, ids[0])]
    got = [x for x in await store.quality_outcomes(T0 + 3, ("judge_reject",), T0 + 5)
           if x[0] == ck]
    assert got == [(ck, True, ids[2])]
    assert (await store.get_claim(ids[0])).decided_ms == T0 + 5


async def test_a_claim_sent_back_undecided_loses_its_decision_time(store):
    hk = "contract-" + uuid.uuid4().hex[:12]
    paid = await store.create_claim(claim(hk))
    back = await store.create_claim(claim(hk))
    for row, status in ((paid, "provisional"), (back, "rejected")):
        assert await store.update_claim(row.id, "received", status=status, decided_ms=T0)
    assert await store.update_claim(paid.id, "provisional", status="settled")
    assert (await store.get_claim(paid.id)).decided_ms == T0
    assert await store.update_claim(back.id, "rejected", status="fetched", reason=None)
    assert (await store.get_claim(back.id)).decided_ms is None


async def test_the_first_trusted_page_needs_a_claim_that_passed_the_judge(store):
    from datetime import date
    hk = "contract-" + uuid.uuid4().hex[:12]
    ev = await store.add_event(EventRecord("merger_acquisition", "finance", date(2026, 10, 13),
                                           "fp-" + uuid.uuid4().hex))
    rows = []
    # Judged and waiting for its line, then still waiting for the judge.
    for i, status in enumerate(("fetched", "novelty")):
        row = await store.create_claim(replace(claim(hk + str(i)), received_ms=T0 + i * 1000))
        assert await store.update_claim(row.id, "received", status=status, trust=0.9,
                                        event_id=ev.id)
        await store.add_source_fetch(SourceFetchRow(claim_id=row.id, verdict="supported",
                                                    published_ms=T0 - 9000 + i * 5000))
        rows.append(row)
    mine = await store.create_claim(claim(hk + "x"))
    assert await store.first_trusted_ms(ev.id, mine.id, 0.75) == T0 - 9000
    assert await store.first_trusted_ms(ev.id, rows[0].id, 0.75) is None


async def test_a_transaction_writes_all_or_nothing(store):
    from app.services.store.rows import LedgerRow
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk))
    assert await store.update_claim(row.id, "received", status="verified")
    paid = LedgerRow(hotkey=hk, claim_id=row.id, event_id=None, kind="provisional", points=1.0,
                     epoch=3, route="event", category="finance")

    with pytest.raises(RuntimeError):
        async with store.transaction() as tx:
            await tx.append_ledger(paid)
            assert await tx.update_claim(row.id, "verified", status="provisional",
                                         decided_ms=T0)
            raise RuntimeError("crash")
    got = await store.get_claim(row.id)
    assert await store.claim_ledger(row.id) == []
    assert (got.status, got.decided_ms) == ("verified", None)

    async with store.transaction() as tx:
        await tx.append_ledger(paid)
        assert await tx.update_claim(row.id, "verified", status="provisional", decided_ms=T0)
    got = await store.get_claim(row.id)
    assert [r.kind for r in await store.claim_ledger(row.id)] == ["provisional"]
    assert (got.status, got.decided_ms) == ("provisional", T0)


async def test_a_claim_is_paid_with_its_row_or_not_at_all(store, monkeypatch):
    from app.services import intake, points
    from tests.pipeline import HEAD
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(claim(hk))
    assert await store.update_claim(row.id, "received", status="verified")
    row = await store.get_claim(row.id)
    f = points.factors("merger_acquisition", "event", 1.0, 1.0)
    real = intake.transition

    async def crash(*args, **kw):
        await real(*args, **kw)
        raise RuntimeError("crash")

    monkeypatch.setattr(intake, "transition", crash)
    with pytest.raises(RuntimeError):
        await points.pay(store, row, "event", f, T0, HEAD)
    got = await store.get_claim(row.id)
    assert await store.claim_ledger(row.id) == []
    assert (got.status, got.decided_ms) == ("verified", None)

    monkeypatch.setattr(intake, "transition", real)
    got = await points.pay(store, row, "event", f, T0, HEAD)
    assert got.status == "provisional"
    assert [r.kind for r in await store.claim_ledger(row.id)] == ["provisional"]
    if hasattr(store, "db"):
        # The claim is stamped by the database clock inside the pay's transaction, once the
        # ledger lock is held (created_at is the transaction's start).
        rows = await store.db.query_raw(
            'SELECT floor(extract(epoch FROM created_at) * 1000)::bigint AS ms '
            'FROM ledger_entry WHERE claim_id = $1::bigint', row.id)
        assert got.decided_ms >= int(rows[0]["ms"])


async def test_source_events_by_claim(store):
    from app.services.store.rows import SourceEventRow, SourceRow
    src = await store.add_source(SourceRow(domain=f"c{uuid.uuid4().hex[:10]}.example",
                                           status="active"))
    a = await store.create_claim(claim("contract-" + uuid.uuid4().hex[:12]))
    b = await store.create_claim(claim("contract-" + uuid.uuid4().hex[:12]))
    for c in (a, b, a):
        await store.add_source_event(SourceEventRow(source_id=src.id, role="changed",
                                                    category="finance", at_ms=T0, claim_id=c.id))
    got = await store.source_events(src.id, claim_id=a.id)
    assert [(e.claim_id, e.role) for e in got] == [(a.id, "changed")] * 2
    assert len(await store.source_events(src.id)) == 3


async def test_an_events_pages_read_in_one_batch(store):
    from app.services.store.rows import SourceEventRow, SourceRow
    a, b, c = [await _event(store, "reported") for _ in range(3)]
    srcs = [await store.add_source(SourceRow(domain=f"c{uuid.uuid4().hex[:10]}.example",
                                             status=s)) for s in ("active", "banned")]
    on_a = await _on(store, a, "provisional", "https://a.example/1")
    on_b = await _on(store, b, "settled", "https://a.example/2")
    await _on(store, b, "rejected", "https://a.example/3", "judge_reject")
    await _on(store, c, "provisional", "https://a.example/4")
    got = await store.claims_for_events([b.id, a.id, a.id], ("provisional", "settled"))
    assert [r.id for r in got] == [on_a.id, on_b.id]
    for ev, row, role in ((a, on_a, "first"), (b, on_b, "confirm"), (c, None, "first")):
        await store.add_source_event(SourceEventRow(
            source_id=srcs[0].id, role=role, category="finance", at_ms=T0, event_id=ev.id,
            claim_id=row.id if row else None, published_ms=T0 + 5))
    got = await store.source_events_for([a.id, b.id])
    assert [(e.event_id, e.claim_id, e.role, e.published_ms) for e in got] == [
        (a.id, on_a.id, "first", T0 + 5), (b.id, on_b.id, "confirm", T0 + 5)]
    assert await store.source_events_for([]) == []
    got = await store.sources_by_ids([srcs[1].id, srcs[0].id, srcs[1].id])
    assert [(s.id, s.status) for s in got] == [(srcs[0].id, "active"), (srcs[1].id, "banned")]
    assert await store.sources_by_ids([]) == []


async def _event(store, confidence: str, event_type: str = "merger_acquisition"):
    from datetime import date
    return await store.add_event(EventRecord(event_type, "finance", date(2026, 10, 13),
                                             "fp-" + uuid.uuid4().hex, confidence=confidence))


async def _on(store, ev, status: str, url: str, reason=None):
    hk = "contract-" + uuid.uuid4().hex[:12]
    row = await store.create_claim(replace(claim(hk), source_url=url))
    assert await store.update_claim(row.id, "received", status=status, reason=reason,
                                    event_id=ev.id)
    return row


async def test_claims_on_a_page_find_its_events_of_one_type_and_date(store):
    from datetime import date
    url = f"https://a.example/{uuid.uuid4().hex}"
    reported, line, gone = [await _event(store, c) for c in ("reported", "line", "reported")]
    other_type = await _event(store, "reported", "funding")
    await _on(store, reported, "provisional", url)
    await _on(store, line, "novelty", url)
    await _on(store, gone, "rejected", url, "judge_reject")
    await _on(store, other_type, "provisional", url)
    await _on(store, gone, "provisional", url + "/other")
    got = await store.events_by_source(url, "merger_acquisition", date(2026, 10, 13))
    assert [e.id for e in got] == [reported.id, line.id]
    assert await store.events_by_source(url, "merger_acquisition", date(2026, 10, 14)) == []


async def test_a_line_left_with_rejected_claims_only_is_deleted(store):
    from app.services.store.rows import LedgerRow, SourceEventRow, SourceRow
    url = f"https://a.example/{uuid.uuid4().hex}"
    dead, live, paid, seen, event = [await _event(store, c)
                                     for c in ("line", "line", "line", "line", "reported")]
    lost = await _on(store, dead, "rejected", url, "doc_prize_used")
    await _on(store, live, "rejected", url, "judge_reject")
    await _on(store, live, "fetched", url)
    await _on(store, seen, "rejected", url, "judge_reject")
    await _on(store, event, "rejected", url, "known_event")
    await store.append_ledger(LedgerRow(hotkey="contract-x", claim_id=None, event_id=paid.id,
                                        kind="debt", points=-1.0, epoch=3))
    src = await store.add_source(SourceRow(domain=f"c{uuid.uuid4().hex[:10]}.example",
                                           status="active"))
    await store.add_source_event(SourceEventRow(source_id=src.id, role="confirm",
                                                category="finance", at_ms=T0, event_id=seen.id))
    mine = {dead.id, live.id, paid.id, seen.id, event.id}
    late = T0 * 2
    assert [i for i in await store.dead_lines(late, 10_000_000) if i in mine] == [dead.id]
    assert [i for i in await store.dead_lines(0) if i in mine] == []
    assert await store.drop_lines([dead.id, live.id, paid.id, seen.id, event.id]) == 1
    assert await store.get_event(seen.id) is not None
    assert await store.get_event(dead.id) is None
    assert (await store.get_claim(lost.id)).event_id is None
    assert await store.get_event(live.id) is not None


async def test_enrichment_rows_claim_finish_and_spend(store):
    from datetime import date

    from app.services.store.rows import EnrichRow

    base = T0 + uuid.uuid4().int % 10**9 * 1000

    async def event(confidence, at, first=True, coldkey="ck-" + uuid.uuid4().hex[:8]):
        ev = await store.add_event(EventRecord(
            event_type="merger_acquisition", category="finance", event_date=date(2026, 10, 1),
            fingerprint=uuid.uuid4().hex, headline="Acme buys Beta", confidence=confidence))
        row = await store.create_claim(claim("contract-" + uuid.uuid4().hex[:12],
                                             coldkey=coldkey))
        await store.update_claim(row.id, "received", status="verified",
                                 route="event" if first else "scoop", event_id=ev.id)
        await store.update_event(ev.id, publish_ms=at,
                                 **({"first_claim_id": row.id} if first else {}))
        return ev.id, coldkey

    rep, _ = await event("reported", base + 1)
    scoop, owner = await event("unverified", base + 2, first=False)
    await event("unverified", base + 3)
    await event("line", base + 4)
    found = await store.enrich_candidates(("confirmed", "reported"), base, base + 10, 50)
    assert [(e.id, o) for e, o in found] == [(rep, None), (scoop, owner)]

    assert await store.add_enrichments([rep], base) == 1
    assert await store.add_enrichments([rep, scoop], base, status="skipped") == 1
    assert [e.id for e, _ in await store.enrich_candidates(
        ("confirmed", "reported"), base, base + 10, 50)] == []
    await store.update_event(scoop, confidence="confirmed")
    assert await store.reopen_enrichments(("confirmed", "reported"), base + 5) == 1

    taken = [r for r in await store.take_enrichments(50, 1000, base + 5, False)
             if r.event_id in (rep, scoop)]
    assert sorted((r.event_id, r.status, r.attempts) for r in taken) == [
        (rep, "running", 1), (scoop, "running", 1)]
    assert [r for r in await store.take_enrichments(50, 1000, base + 7, False)
            if r.event_id in (rep, scoop)] == []

    assert await store.defer_enrichment(scoop, 1, base + 100, "HTTP 404", count=False)
    got = await store.get_enrichment(scoop)
    assert (got.status, got.attempts, got.run_ms, got.error) == ("pending", 0, base + 100,
                                                                "HTTP 404")

    done = EnrichRow(event_id=rep, status="done", attempts=1, run_ms=base, confidence="reported",
                     summary="Acme bought Beta.", sentiment="positive", sentiment_score=0.4,
                     country="US", region="Ohio", cost_usd=0.00024, done_ms=base + 8)
    assert not await store.finish_enrichment(EnrichRow(**{**done.__dict__, "attempts": 2}))
    assert await store.finish_enrichment(done, [0.5] * 384)
    assert not await store.finish_enrichment(done)
    got = await store.get_enrichment(rep)
    assert (got.status, got.summary, got.sentiment, got.sentiment_score, got.country,
            got.region, got.cost_usd, got.done_ms) == (
        "done", "Acme bought Beta.", "positive", 0.4, "US", "Ohio", 0.00024, base + 8)
    if hasattr(store, "db"):
        (r,) = await store.db.query_raw(
            "SELECT vector_dims(embedding) AS n FROM events WHERE id = $1", rep)
        assert r["n"] == 384

    day = date(2030, 1, 1 + uuid.uuid4().int % 28)
    before = await store.enrich_spend(day)
    await store.add_enrich_spend(day, 0.25)
    await store.add_enrich_spend(day, 0.5)
    assert await store.enrich_spend(day) == pytest.approx(before + 0.75)
