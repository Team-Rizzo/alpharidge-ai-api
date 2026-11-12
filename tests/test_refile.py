"""Claims on a filing its subject made, placed as trusted claims after they were held as
scoops or turned away as low trust."""

from dataclasses import replace
from datetime import date

import pytest

from app.services import ledger, resolve, trust
from app.services.store import (
    EventRecord, MemoryStore, ScoopCandidateRow, SourceFetchRow, SourceRow,
)
from tests.test_verifier import RECEIVED_MS, submit

URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/a8k.htm"
HEAD = 9_480_000


async def placed(store, status, reason=None, confidence="unverified", judged="pass"):
    src = await store.add_source(SourceRow(domain="sec.gov", owner_id="sec", status="active",
                                           base_trust=0.7))
    row = await submit(store, URL, route="scoop", trusted=False)
    ev = await store.add_event(EventRecord("merger_acquisition", "finance", date(2026, 10, 13),
                                           f"fp{row.id}", confidence=confidence,
                                           first_seen_ms=RECEIVED_MS))
    await store.update_claim(row.id, "novelty", status=status, reason=reason, event_id=ev.id,
                             source_id=src.id, trust=0.1, trust_band="low")
    await store.add_source_fetch(SourceFetchRow(row.id, "supported", content_hash="h",
                                                check={"judge": {"outcome": judged}}))
    return await store.get_claim(row.id), ev


@pytest.fixture
def filed(monkeypatch):
    def set_trust(t):
        async def of_claim(*a, **k):
            return trust.ClaimTrust(t, "primary" if t >= 1 else "low", t >= 1,
                                    "0000000001" if t >= 1 else None, t >= 1)
        monkeypatch.setattr(trust, "of_claim", of_claim)
    set_trust(1.0)
    return set_trust


async def test_a_held_filing_scoop_is_paid_as_its_event(filed):
    store = MemoryStore()
    row, ev = await placed(store, "verified")
    await store.add_scoop_candidate(ScoopCandidateRow(row.id, "https://example.org/x", "v", 1))
    assert await resolve.Resolver().refile(store, row, RECEIVED_MS + 1000, HEAD) == "paid"
    row = await store.get_claim(row.id)
    assert (row.status, row.route, row.trust) == ("provisional", "event", 1.0)
    assert (await store.get_event(ev.id)).first_claim_id == row.id
    (paid,) = [r for r in await store.claim_ledger(row.id) if r.kind == ledger.PROVISIONAL]
    assert paid.route == "event" and paid.points > 0
    assert [c.status for c in await store.scoop_candidates(row.id)] == ["closed"]


async def test_a_low_trust_rejection_goes_back_to_its_open_line(filed):
    store = MemoryStore()
    row, _ = await placed(store, "rejected", "low_trust", confidence="line")
    assert await resolve.Resolver().refile(store, row, RECEIVED_MS + 1000, HEAD) == "reopened"
    row = await store.get_claim(row.id)
    assert (row.status, row.reason, row.trust) == ("fetched", None, 1.0)
    await resolve.Resolver().run(store, RECEIVED_MS + 2000, HEAD)
    row = await store.get_claim(row.id)
    assert (row.status, row.route) == ("provisional", "event")
    assert [r.route for r in await store.claim_ledger(row.id)] == ["event"]


async def test_nothing_changes_unless_the_subject_filed_it_and_the_event_is_open(filed):
    store = MemoryStore()
    row, ev = await placed(store, "rejected", "low_trust", confidence="reported")
    assert await resolve.Resolver().refile(store, row, RECEIVED_MS, HEAD) == "event_taken"
    unjudged, _ = await placed(store, "verified", judged="reject")
    assert await resolve.Resolver().refile(store, unjudged, RECEIVED_MS, HEAD) == "no_judge_pass"
    filed(0.1)
    held, _ = await placed(store, "verified")
    assert await resolve.Resolver().refile(store, held, RECEIVED_MS, HEAD) == \
        "not_filed_by_subject"
    assert (await store.get_claim(held.id)).route == "scoop"
