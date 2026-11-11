"""Signed claim receipts: one per stored claim, returned on POST and shown on reads."""

import asyncio

from bittensor_wallet import Keypair

from alpharidge_ai.ledger import receipts as rc
from alpharidge_ai.schema import canonical_body
from tests.test_claims import (MINER, RIVAL, _accept, claim, client, post, signed,  # noqa: F401
                               store)

PUBKEY = Keypair.create_from_seed("0x" + "7e" * 32).ss58_address
T0 = 1_790_000_000_000


async def test_concurrent_claims_each_get_a_linked_receipt(store):
    rows = await asyncio.gather(*[
        _accept(store, MINER if i % 2 else RIVAL, T0 + 20 - i, headline=f"h{i}")
        for i in range(20)])
    got = await store.receipts_for([r.id for r in rows])
    by_seq = sorted(got.values(), key=lambda r: r["seq"])
    assert [r["seq"] for r in by_seq] == list(range(1, 21))
    assert by_seq[0]["prev"] == rc.GENESIS
    assert all(b["prev"] == a["hash"] for a, b in zip(by_seq, by_seq[1:]))
    for row in rows:
        r = got[row.id]
        assert rc.valid(r, PUBKEY)
        assert rc.matches(r, row.id, row.hotkey, row.body_hash, row.received_ms)


def test_post_returns_the_receipt_and_the_claim_read_shows_it(client, store):
    first = post(client, MINER, canonical_body(claim())).json()
    again = post(client, MINER, canonical_body(claim())).json()
    r = first["receipt"]
    assert again["receipt"] == r and rc.valid(r, PUBKEY) and r["claim_id"] == first["id"]
    path = f"/claims/{first['id']}"
    read = client.get(path, headers=signed(MINER, "GET", path))
    assert read.json()["receipt"] == r
