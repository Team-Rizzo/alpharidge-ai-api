"""Re-submissions of an indexed event come out known."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from alpharidge_ai.schema import Claim
from app.services import novelty
from app.services.entities import EntityIndex, PlaceIndex
from app.services.store import ClaimRow, MemoryStore

FIXTURES = Path(__file__).parent / "fixtures" / "replays"
ENTITIES = FIXTURES.parent / "entities.json"
NOW_MS = 1_760_400_000_000
PAID_AS_NEW = {"event", "scoop"}


@pytest.fixture(scope="module")
def entities():
    return EntityIndex.from_seed(overrides=ENTITIES)


async def index(store, claim, entities, places=PlaceIndex()):
    rec = await novelty.open_line(store, claim, entities, places)
    store.events[rec.id] = replace(rec, confidence="reported", first_claim_id=rec.id)
    return rec


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
async def test_replay_is_known(path, entities):
    case = json.loads(path.read_text())
    store = MemoryStore()
    for c in case["index"]:
        await index(store, Claim(**c), entities)
    n = await novelty.decide(store, Claim(**case["replay"]), entities, PlaceIndex(), NOW_MS,
                             trust=0.9)
    assert n.state == case["expect"], n
    assert n.route not in PAID_AS_NEW


async def test_genuinely_different_event_is_new(entities):
    base = json.loads((FIXTURES / "case_05.json").read_text())["index"][0]
    store = MemoryStore()
    await index(store, Claim(**base), entities)
    other = dict(base, headline="Globex agrees to buy Seagrove Bio for $3 billion",
                 actors=[{"id_scheme": "wikidata", "id": "Q99999002", "role": "acquirer",
                          "name": "Globex"},
                         {"id_scheme": "other", "id": "seagrove", "role": "target",
                          "name": "Seagrove Bio"}],
                 facts=[{"field": "deal_value", "value": 3.0, "unit": "usd_billions"}],
                 source_url="https://news.example.com/globex")
    n = await novelty.decide(store, Claim(**other), entities, PlaceIndex(), NOW_MS, trust=0.9)
    assert (n.state, n.route) == ("new", "event")
    n = await novelty.decide(store, Claim(**other), entities, PlaceIndex(), NOW_MS, trust=0.2)
    assert (n.state, n.route) == ("new", "scoop")


async def test_roundup_page_pays_one_prize():
    store = MemoryStore()
    page = " ".join(f"The department awarded ${i + 2} million to college {i} for workforce "
                    f"training in region {i % 7}." for i in range(40))
    doc = novelty.doc_hash("https://www.example.gov/news/grants", page)
    assert await novelty.doc_prize_open(store, doc, NOW_MS)
    store.claims[1] = claim_row({"kind": "event", "event_type": "funding", "source_url": ""}, 1,
                                status="verified", route="event", doc_hash=doc,
                                received_ms=NOW_MS - 1000)
    copy = novelty.doc_hash("https://mirror.example.org/grants", page + " Reprinted.")
    assert not await novelty.doc_prize_open(store, doc, NOW_MS)
    assert not await novelty.doc_prize_open(store, copy, NOW_MS)
    assert await novelty.doc_prize_open(store, doc, NOW_MS, listing=True)

    framed = ("Subscribe to our newsletter for the latest regional updates and offers. "
              + " ".join(page.split()[:-25]) + " Share this story. Related: more grant news.")
    assert novelty.same_doc(doc, novelty.doc_hash("https://portal.example.net/g", framed))
    other = " ".join(f"The city council approved {i} new bus routes for district {i}."
                     for i in range(40))
    assert not novelty.same_doc(doc, novelty.doc_hash("https://example.org/bus", other))


def test_wire_copies_share_a_lead():
    lead = ("NEW YORK (Reuters) - Acme agreed on Tuesday to buy Beta Systems for $1.2 billion "
            "in cash, the companies said.")
    a = novelty.doc_hash("https://a.example.com/x", "Markets live. " + lead + " Shares rose.")
    b = novelty.doc_hash("https://b.example.net/y", lead + " Analysts were split on the price, "
                         "and several funds said they would wait for the filing.")
    assert novelty.same_doc(a, b)


def claim_row(payload, i, **kw):
    fields = dict(hotkey="h", signature="", nonce=str(i), signed_at=0, body_hash=str(i), body="",
                  received_ms=NOW_MS, bucket=0, kind=payload["kind"], category="finance",
                  event_type=payload["event_type"], payload=payload,
                  source_url=payload["source_url"], id=i)
    fields.update(kw)
    return ClaimRow(**fields)
