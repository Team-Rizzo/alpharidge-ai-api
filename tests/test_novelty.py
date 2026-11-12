"""Novelty: identity keys, subjects, lines, known events, contrastive check, duplicates."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from alpharidge_ai.schema import Claim
from app.services import novelty
from app.services.entities import EntityIndex, PlaceIndex
from app.services.store import MemoryStore
from tests.pipeline import HOUR_MS, T0, Lane
from tests.test_replays import ENTITIES, FIXTURES, NOW_MS, claim_row, index

H = 3600 * 1000
BASE = json.loads((FIXTURES / "case_05.json").read_text())["index"][0]


def other(name, id_=None, role="target"):
    return {"id_scheme": "other", "id": id_ or name.lower().replace(" ", "-"), "name": name,
            "role": role}


@pytest.fixture(scope="module")
def entities():
    return EntityIndex.from_seed(overrides=ENTITIES)


async def _decide(store, claim, entities, trust=0.9):
    return await novelty.decide(store, Claim(**claim), entities, PlaceIndex(), NOW_MS,
                                trust=trust)


def test_fingerprint_ignores_actor_scheme_and_order(entities):
    by_qid = Claim(**BASE)
    by_cik = Claim(**dict(BASE, actors=[BASE["actors"][1],
                                        {"id_scheme": "cik", "id": "0000999904", "name": "UMBR"}]))
    places = PlaceIndex()
    assert (novelty.identity(by_qid, entities, places).fingerprint
            == novelty.identity(by_cik, entities, places).fingerprint)


async def test_the_subject_decides_identity(entities):
    store = MemoryStore()
    await index(store, Claim(**BASE), entities)
    paraphrase = dict(BASE, headline="Nimbus Games sold", actors=[other("Nimbus Games")])
    assert (await _decide(store, paraphrase, entities)).state == "known"

    other_target = dict(BASE, headline="Umbrella agrees to acquire Orbit Studios",
                        actors=[BASE["actors"][0], other("Orbit Studios")],
                        facts=[{"field": "deal_value", "value": 3.0, "unit": "usd_billions"}])
    assert (await _decide(store, other_target, entities)).state == "new"

    by_fact = [dict(BASE, actors=[BASE["actors"][0]], facts=[{"field": "target", "value": t}])
               for t in ("Nimbus Games", "Orbit Studios")]
    assert (novelty.identity(Claim(**by_fact[0]), entities, PlaceIndex()).fingerprint
            != novelty.identity(Claim(**by_fact[1]), entities, PlaceIndex()).fingerprint)


async def test_single_actor_events_of_one_type_are_distinct(entities):
    store = MemoryStore()
    suit = dict(BASE, event_type="legal", headline="Kestrel Labs sued over patents", stage=None,
                facts=[], actors=[other("Kestrel Labs", role="party")])
    await index(store, Claim(**suit), entities)
    rival = dict(suit, headline="Redwood Labs sued over patents",
                 actors=[other("Redwood Labs", role="party")])
    n = await _decide(store, rival, entities)
    assert (n.state, n.route) == ("new", "event")


async def test_a_shifted_date_still_finds_the_event(entities):
    store = MemoryStore()
    await index(store, Claim(**BASE), entities)
    day = Claim(**BASE).event_date
    shifted = dict(BASE, event_date=(day - timedelta(days=5)).isoformat())
    assert (await _decide(store, shifted, entities)).state == "known"


async def test_claims_of_a_three_week_old_event_collide(entities):
    store = MemoryStore()
    old = (Claim(**BASE).event_date - timedelta(days=21)).isoformat()
    first = dict(BASE, event_date=old)
    assert (await _decide(store, first, entities)).state == "new"
    await index(store, Claim(**first), entities)
    again = dict(first, headline="Nimbus Games sold", actors=[other("Nimbus Games")])
    assert (await _decide(store, again, entities)).reason == "known_event"


async def test_missing_required_fields_is_invalid(entities):
    claim = dict(BASE, event_type="earnings", headline="Globex posts quarterly results",
                 actors=[other("Globex")], facts=[], stage=None)
    n = await _decide(MemoryStore(), claim, entities)
    assert (n.state, n.route, n.reason) == ("known", None, "invalid_schema")


async def test_ids_and_types_attach_only_to_their_own_event(entities):
    store = MemoryStore()
    ev = await index(store, Claim(**BASE), entities)
    held = dict(BASE, hard_id="sec:0001")
    store.events[ev.id].identity_key = "sec:0001"
    halt = dict(held, event_type="trading_halt", stage=None, facts=[])
    assert (await _decide(store, halt, entities)).reason == "unsupported"

    partner = dict(BASE, event_type="partnership", stage=None, facts=[])
    assert (await _decide(store, partner, entities)).state == "known"


async def test_first_claim_opens_a_line_that_rivals_join(entities):
    store = MemoryStore()
    first = await store.create_claim(claim_row(BASE, 1))
    await store.update_claim(first.id, "received", status="queued")
    row = await novelty.apply(store, await store.get_claim(first.id), entities, PlaceIndex(),
                              NOW_MS, trust=0.9)
    assert (row.status, row.novelty, row.route) == ("novelty", "new", "event")
    assert store.events[row.event_id].confidence == "line"

    rival = dict(BASE, headline="Umbrella buys Nimbus Games")
    n = await _decide(store, rival, entities)
    assert (n.state, n.event_id, n.line) == ("new", row.event_id, True)


async def test_a_known_event_pays_nothing_more(entities):
    store = MemoryStore()
    await index(store, Claim(**BASE), entities)
    n = await _decide(store, dict(BASE, stage="approved",
                                  facts=[{"field": "expected_close", "value": "Q1 2027"}]),
                      entities)
    assert (n.state, n.route, n.reason) == ("known", None, "known_event")


async def test_same_figures_a_day_off_are_one_event_but_rate_decisions_stay_apart(entities):
    store = MemoryStore()
    first = dict(BASE, actors=[other("Kestrel Labs", role="acquirer"), other("Nimbus Games")])
    await index(store, Claim(**first), entities)
    day = Claim(**BASE).event_date
    copy = dict(first, event_date=(day + timedelta(days=1)).isoformat(),
                actors=[other("Kestrel Laboratories", role="acquirer"), other("Nimbus Studio")])
    assert (await _decide(store, copy, entities)).state == "known"

    hold = dict(BASE, event_type="monetary_policy", stage=None, place={"iso2": "US"},
                actors=[other("Federal Reserve", role="central_bank")],
                facts=[{"field": "change_bps", "value": 0, "unit": "bps"}])
    await index(store, Claim(**hold), entities)
    other_bank = dict(hold, event_date=(day + timedelta(days=1)).isoformat(), place=None,
                      actors=[other("Reserve Bank of Fiji", role="central_bank")])
    assert (await _decide(store, other_bank, entities)).state == "new"


async def test_one_shared_figure_never_joins_two_named_companies(entities):
    def results(actor, revenue):
        return dict(BASE, event_type="earnings", headline="x", stage=None, actors=[actor],
                    facts=[{"field": "period", "value": "third quarter"},
                           {"field": "revenue", "value": revenue, "unit": "usd_millions"}])
    store = MemoryStore()
    await index(store, Claim(**results(other("Frontera Energy Corporation", role="company"),
                                       33.0)), entities)
    filer = {"id_scheme": "cik", "id": "0000722313", "name": "Nortech Systems",
             "role": "company"}
    assert (await _decide(store, results(filer, 33.0), entities)).state == "new"
    assert (await _decide(store, results(other("Allot Ltd", role="company"), 33.1),
                          entities)).state == "new"
    copy = results(other("Frontera Energy", role="company"), 33.0)
    assert (await _decide(store, copy, entities)).state == "known"


def _release(indicator, actual, measure):
    return {"kind": "event", "headline": "x", "event_type": "economic_data",
            "event_date": "2026-10-15", "place": {"iso2": "US"},
            "source_url": "https://a.example/x",
            "actors": [other("Bureau of Labor Statistics", role="publisher")],
            "facts": [{"field": "indicator", "value": indicator},
                      {"field": "actual", "value": actual, "unit": "percent"},
                      {"field": "measure", "value": measure}]}


async def test_an_indicator_keys_by_its_code(entities):
    store = MemoryStore()
    await index(store, Claim(**_release("Consumer Price Index", 3.0, "yoy")), entities)
    assert (await _decide(store, _release("CPI", 0.3, "mom"), entities)).state == "known"
    assert (await _decide(store, _release("core CPI", 0.2, "mom"), entities)).state == "new"


async def test_only_a_verified_cik_keys_an_event(entities):
    def er(name, eps):
        return dict(BASE, event_type="earnings", headline="x", stage=None,
                    actors=[{"id_scheme": "cik", "id": "0001023128", "name": name,
                             "role": "company"}],
                    facts=[{"field": "period", "value": "Q3 2026"},
                           {"field": "eps", "value": eps, "unit": "usd_per_unit"}])
    store = MemoryStore()
    await index(store, Claim(**er("Kestrel Motors Inc", 9.5)), entities)
    assert (await _decide(store, er("Kestrel & Drive", 9.6), entities)).state == "new"

    store = MemoryStore()
    rec = await novelty.open_line(store, Claim(**er("Kestrel Motors Inc", 9.5)), entities,
                                  PlaceIndex(), filer="0001023128")
    store.events[rec.id] = replace(rec, confidence="reported", first_claim_id=rec.id)
    n = await _decide(store, er("Kestrel & Drive", 9.6), entities)
    assert (n.state, n.event_id) == ("known", rec.id)


async def test_contrastive_names_the_known_event(entities):
    store = MemoryStore()
    ev = await index(store, Claim(**BASE), entities)
    cands = [store.events[ev.id]]
    assert novelty.contrastive("The Nimbus Games acquisition closes next year.", cands) == ev.id
    assert novelty.contrastive("Umbrella agreed to acquire Orbit Studios.", cands) is None
    page = "Umbrella agreed to acquire Orbit Studios, a year after the Nimbus Games acquisition."
    assert novelty.contrastive(page, cands, ["Orbit Studios"]) is None
    assert novelty.contrastive(page, cands, ["Nimbus"]) == ev.id
    ticker = replace(cands[0], subject_keys=("name:a",))
    page = "A. O. Smith agreed to acquire a rival in a deal, the company said."
    assert novelty.contrastive(page, [ticker], ["A. O. Smith"]) is None


async def test_reworded_copy_of_the_winning_page_pays_once():
    lane = Lane(check=True)
    await lane.sources()
    ev = await lane.submit("m1", T0, source_url="https://newsone.com/e1")
    await lane.step(T0 + 1000)
    assert lane.store.events[lane.event_of(ev)].stage == "announced"
    copy = await lane.submit("m3", T0 + 10_000, headline="Acme Inc. to buy Beta Labs",
                             actors=[{"id_scheme": "other", "id": "acme", "role": "acquirer",
                                      "name": "Acme Inc."},
                                     {"id_scheme": "other", "id": "beta-labs", "role": "target",
                                      "name": "Beta Labs"}],
                             source_url="https://newsone.com/e1")
    await lane.step(T0 + 20_000)
    await lane.settle(T0 + 80 * HOUR_MS)
    assert lane.store.claims[copy.id].reason == "doc_prize_used"
    assert "m3" not in lane.paid()


async def test_unestablished_registry_id_counts_as_known():
    from app.services.entities import EntityRecord

    idx = EntityIndex([EntityRecord("wikidata", "Q900", "Kestrel Partners", "company",
                                    status="unclear", origin="registry")])
    claim = dict(BASE, actors=[BASE["actors"][0], {"id_scheme": "wikidata", "id": "Q900",
                                                   "role": "target", "name": "Kestrel Partners"}])
    n = await _decide(MemoryStore(), claim, idx, trust=0.3)
    assert (n.state, n.route, n.reason) == ("known", None, "unclear_entity")
    assert (await _decide(MemoryStore(), claim, idx, trust=0.9)).state == "new"


async def test_a_name_only_one_day_event_dated_a_day_off_is_one_event(entities):
    store = MemoryStore()
    halt = dict(BASE, event_type="trading_halt", headline="Kestrel Labs halted", stage=None,
                actors=[other("Kestrel Labs", role="company")],
                facts=[{"field": "exchange", "value": "Nasdaq"}])
    await index(store, Claim(**halt), entities)
    day = Claim(**BASE).event_date
    late = dict(halt, event_date=(day + timedelta(days=1)).isoformat())
    assert (await _decide(store, late, entities)).state == "known"


async def test_a_ticker_written_as_the_company_is_the_same_event(entities, monkeypatch):
    from app.services import entities as entities_mod
    sec = entities_mod.SecNames()
    sec.update([("1494904", "GBLI", "Global Indemnity Group, LLC")])
    monkeypatch.setattr(entities_mod, "SEC_NAMES", sec)
    store = MemoryStore()
    ceo = [{"field": "person", "value": "Jessica Buss"}, {"field": "role", "value": "ceo"},
           {"field": "direction", "value": "appointed"}]
    first = dict(BASE, event_type="personnel", stage=None, headline="GBLI names CEO",
                 actors=[other("GBLI", role="company")], facts=ceo)
    await index(store, Claim(**first), entities)
    later = dict(first, headline="Global Indemnity Group names CEO",
                 actors=[other("Global Indemnity Group", role="company")])
    n = await _decide(store, later, entities)
    assert (n.state, n.reason) == ("known", "known_event")
