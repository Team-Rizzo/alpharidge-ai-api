"""The memory store used by the tests and the Prisma store give the same results. The Prisma
side runs with STORE_TEST_DATABASE_URL set to a scratch database (the local e2e run does)."""

import os
import uuid
from dataclasses import replace

import pytest

from app.services.store import MemoryStore
from app.services.store.rows import (
    ClaimRow, JudgeItemRow, JudgePostRow, ReverifyRow, ScoopCandidateRow, SourceFetchRow,
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
