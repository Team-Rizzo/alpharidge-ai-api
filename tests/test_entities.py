"""Entity alias linking and coarse place keys."""

from alpharidge_ai.schema import Actor, Place
from app.services.entities import (
    EntityIndex, EntityRecord, PlaceIndex, normalize_name, places_match,
    read_seed,
)

ACME = EntityRecord("wikidata", "Q99999001", "Acme Inc.", "company", ("Acme",),
                     ("cik:0000999901", "lei:00000000000000ACME01", "ticker:ACME"))
FED = EntityRecord("wikidata", "Q53536", "Federal Reserve", "central_bank", ("the Fed", "Fed"))
ACME_RECORDS = EntityRecord("other", "acme-records", "Acme Records", aliases=("Acme",))


def actor(scheme, id, name=None):
    return Actor(id_scheme=scheme, id=id, name=name)


def test_normalize_name_folds_case_punctuation_and_suffixes():
    assert normalize_name("The Acme-Widget Company") == "acme widget"
    assert normalize_name("McDonald’s Corp.") == "mcdonalds"
    assert normalize_name("Atlético Madrid") == "atletico madrid"
    assert normalize_name("S&P 500") == normalize_name("S and P 500")


def test_link_by_id_xref_and_alias():
    idx = EntityIndex([ACME, FED])
    assert idx.link(actor("wikidata", "q99999001", "Acme")).entity is ACME
    assert idx.link(actor("cik", "999901", "ACME")).entity is ACME
    hit = idx.link(actor("other", "x", "ACME INC"))
    assert hit.entity is ACME and hit.via == "alias"
    assert idx.link(actor("other", "the fed")).entity is FED
    assert idx.link(actor("wikidata", "Q999")) is None


def test_an_id_links_only_under_its_own_name():
    idx = EntityIndex([ACME, FED])
    assert idx.link(actor("wikidata", "Q99999001", "Gamma Corp")) is None
    assert idx.actor_keys(actor("wikidata", "Q99999001", "Gamma Corp")) == {"name:gamma"}
    assert idx.actor_keys(actor("wikidata", "Q999", "Gamma Corp")) == {"name:gamma"}
    assert not idx.bound(actor("wikidata", "Q999", "Gamma Corp"))
    assert idx.bound(actor("wikidata", "Q99999001", "Acme Inc."))


def test_shared_alias_prefers_the_cross_referenced_record():
    idx = EntityIndex([ACME, ACME_RECORDS])
    assert idx.link(actor("other", "x", "Acme")).entity is ACME
    assert idx.link(actor("other", "x", "Acme Records")).entity is ACME_RECORDS
    plain = EntityRecord("other", "acme-band", "Acme", aliases=("Acme",))
    assert EntityIndex([ACME_RECORDS, plain]).link(actor("other", "x", "Acme")) is None


def test_actor_keys_overlap_across_schemes():
    idx = EntityIndex([ACME])
    a = idx.actor_keys(actor("cik", "0000999901", "Acme"))
    b = idx.actor_keys(actor("other", "x", "Acme Inc"))
    assert "wikidata:Q99999001" in a & b


def test_seed_file_covers_launch_categories():
    rows = read_seed()
    kinds = {r.kind for r in rows}
    assert {"company", "central_bank", "statistics_agency", "league", "team"} <= kinds
    assert sum(r.kind == "company" for r in rows) >= 200
    idx = EntityIndex(rows)
    assert idx.link(actor("other", "x", "FOMC")).entity.external_id == "Q53536"
    assert idx.link(actor("other", "x", "Premier League")).entity.kind == "league"
    assert len(idx) == len(rows)


def test_place_keys_are_country_level():
    places = PlaceIndex()
    assert places.place_key(Place(iso2="US", admin1="UT"), "admin1") == "US"
    assert places.place_key(None, "none") == "" and places.place_key(None, "country") is None
    assert places_match("US-UT", "US") and not places_match("US-UT", "US-CA")


DAY = 86400 * 1000
NOW = 1_800_000_000_000


def _fixture_registry():
    from app.services.registry import FixtureResolver, RegistryRecord as R

    old = dict(established=True, classes=("Q4830453",))
    moved = R("wikidata", "Q108", "Delta Inc", website="https://delta.example",
              redirected_from="Q107", **old)
    fixtures = FixtureResolver([
        R("wikidata", "Q100", "Acme Corp", xrefs=("cik:123",), website="https://acme.com", **old),
        R("cik", "0000000123", "ACME CORP", established=True),
        R("wikidata", "Q101", "Acme Labs", website="https://acmelabs.com", **old),
        R("wikidata", "Q102", "Kestrel Partners"),
        R("wikidata", "Q103", "Gamma Corp", xrefs=("cik:999",), **old),
        R("cik", "0000000999", "Other Co", established=True),
        R("wikidata", "Q104", "Target", established=True, classes=("Q11424",),
          website="https://target.example"),
        R("wikidata", "Q105", "Jane Roe", established=True, classes=("Q5",),
          offices=("Q11696",)),
        R("wikidata", "Q106", "John Doe", established=True, classes=("Q5",),
          offices=("Q11696",), sitelinks=3),
        R("wikidata", "Q109", "Omega Motors", xrefs=("cik:777",), **dict(old, classes=())),
        R("cik", "0000000777", "Omega Motors", established=True),
    ])
    fixtures.add(moved, key="wikidata:Q107")
    return fixtures


async def _resolve(reg, qid):
    from app.services.registry import WIKIDATA

    return await reg.resolve(WIKIDATA, qid, NOW)


async def test_registry_records_are_vetted_before_they_link():
    from alpharidge_ai.schema import EventType
    from app.services.registry import Registry

    reg = Registry(_fixture_registry())
    acme, labs, young, gamma, film, jane, john = [
        await _resolve(reg, q) for q in ("Q100", "Q101", "Q102", "Q103", "Q104", "Q105", "Q106")]
    assert acme.status == "linked" and acme.website and "cik:0000000123" in acme.keys
    assert labs.status == "linked" and labs.website is None
    assert gamma.status == "unclear" and gamma.keys == ("wikidata:Q103",)
    assert {young.status, film.status, jane.status} == {"unclear"}
    assert john.status == "linked" and john.kind == "official" and john.roles
    moved = await _resolve(reg, "Q107")
    assert moved.key == "wikidata:Q108" and "wikidata:Q107" in moved.keys

    omega = await _resolve(reg, "Q109")
    assert omega.status == "unclear" and not EntityIndex([omega]).has("cik", "777")

    idx = EntityIndex([acme, young, film])
    assert idx.bound(actor("cik", "123", "Acme Corp"), EventType("earnings"))
    assert not idx.bound(actor("wikidata", "Q100", "Acme Corp"), EventType("monetary_policy"))
    assert idx.unclear(actor("wikidata", "Q102", "Kestrel Partners"))
    keys = idx.actor_keys(actor("wikidata", "Q102", "Kestrel Partners"))
    assert keys == {"name:kestrel partners"}
    assert idx.link(actor("other", "x", "Target")) is None


async def test_registry_outage_holds_the_claim_and_retries():
    from app.services.registry import Registry, RegistryUnavailable
    from app.services.store import MemoryStore

    fixtures = _fixture_registry()
    reg, store, idx = Registry(fixtures), MemoryStore(), EntityIndex()
    acme = [actor("wikidata", "Q101", "Acme Labs")]
    fixtures.add(RegistryUnavailable("timeout"), key="wikidata:Q101")
    assert not await reg.ensure(store, idx, acme, NOW, NOW)
    calls = len(fixtures.calls)
    assert not await reg.ensure(store, idx, acme, NOW + 1000, NOW)
    assert len(fixtures.calls) == calls
    assert await reg.ensure(store, idx, acme, NOW + DAY, NOW) and not idx.has("wikidata", "Q101")

    fixtures.add(_fixture_registry().records["wikidata:Q101"])
    assert await reg.ensure(store, idx, acme, NOW + DAY, NOW + DAY)
    assert idx.bound(acme[0]) and await store.registry_entities()


async def test_unclear_records_are_looked_up_again_and_anchors_are_not_shared():
    from app.services.registry import Registry, RegistryRecord as R
    from app.services.store import MemoryStore

    fixtures = _fixture_registry()
    reg, store, idx = Registry(fixtures), MemoryStore(), EntityIndex()
    kestrel = [actor("wikidata", "Q102", "Kestrel Partners")]
    assert await reg.ensure(store, idx, kestrel, NOW) and idx.unclear(kestrel[0])
    fixtures.add(R("wikidata", "Q102", "Kestrel Partners", established=True,
                   classes=("Q4830453",), website="https://kestrel.example"))
    assert await reg.ensure(store, idx, kestrel, NOW + 1000) and idx.unclear(kestrel[0])
    assert await reg.ensure(store, idx, kestrel, NOW + 2 * DAY) and idx.bound(kestrel[0])
    assert (await store.registry_entities())[0].status == "linked"

    await reg.ensure(store, idx, [actor("wikidata", "Q100", "Acme Corp")], NOW)
    fixtures.add(R("wikidata", "Q110", "Acme Corp", xrefs=("cik:123",), established=True,
                   classes=("Q4830453",), website="https://acme-ir.example"))
    dup = await reg.resolve("wikidata", "Q110", NOW, idx)
    assert dup.website is None and "cik:0000000123" not in dup.keys


async def test_a_slow_lookup_holds_its_claim_without_holding_the_caller():
    import asyncio
    import time

    from app.services.registry import Registry
    from app.services.store import MemoryStore

    fixtures, gate = _fixture_registry(), asyncio.Event()

    class Slow:
        async def fetch(self, scheme, ext, before_ms):
            await gate.wait()
            return await fixtures.fetch(scheme, ext, before_ms)

    reg, store, idx = Registry(Slow()), MemoryStore(), EntityIndex()
    acme = [actor("wikidata", "Q101", "Acme Labs")]
    t = time.monotonic()
    assert not await reg.ensure(store, idx, acme, NOW, NOW)
    assert not await reg.ensure(store, idx, acme, NOW + 1, NOW)
    assert time.monotonic() - t < 1 and len(reg._pending) == 1
    gate.set()
    await asyncio.gather(*reg._pending.values())
    assert await reg.ensure(store, idx, acme, NOW + 2, NOW) and idx.bound(acme[0])


async def test_a_failed_entity_write_is_retried_like_a_failed_lookup():
    import asyncio

    from app.services.registry import Registry, id_key
    from app.services.store import MemoryStore

    store, idx = MemoryStore(), EntityIndex()
    reg = Registry(_fixture_registry())
    real, fail = store.put_entity, [True]

    async def put_entity(rec):
        if fail.pop() if fail else False:
            raise RuntimeError("db down")
        return await real(rec)

    store.put_entity = put_entity
    acme = [actor("wikidata", "Q101", "Acme Labs")]
    assert not await reg.ensure(store, idx, acme, NOW, NOW)
    await asyncio.gather(*reg._pending.values())
    assert not await reg.ensure(store, idx, acme, NOW + 1, NOW)
    assert id_key("wikidata", "Q101") in reg._retry and not idx.has("wikidata", "Q101")
    later = NOW + 61_000
    if not await reg.ensure(store, idx, acme, later, NOW):
        await asyncio.gather(*reg._pending.values())
        assert await reg.ensure(store, idx, acme, later + 1, NOW)
    assert idx.bound(acme[0]) and await store.registry_entities()


async def test_public_figures_states_and_startups_link_from_the_registry():
    from alpharidge_ai.schema import EventType
    from app.services.registry import FixtureResolver, Registry, RegistryRecord as R

    old = dict(established=True)
    reg = Registry(FixtureResolver([
        R("wikidata", "Q201", "Rowan Hale", classes=("Q5",), offices=("Q4416090",),
          sitelinks=40, **old),
        R("wikidata", "Q202", "Ines Varga", classes=("Q5",), teams=("Q8682",), sitelinks=30,
          **old),
        R("wikidata", "Q203", "Pat Moreno", classes=("Q5",), sitelinks=5, **old),
        R("wikidata", "Q204", "Freedonia", classes=("Q3624078", "Q6256"), sitelinks=300, **old),
        R("wikidata", "Q205", "Lumen Labs", classes=("Q1058914", "Q129238"),
          website="https://lumen.example", **old),
        R("wikidata", "Q206", "Tiny State", classes=("Q6256",), sitelinks=2, **old),
    ]))
    hale, varga, moreno, freedonia, lumen, tiny = [
        await _resolve(reg, f"Q20{i}") for i in range(1, 7)]
    assert (hale.kind, hale.status, hale.roles) == ("official", "linked", ("office:Q4416090",))
    assert (varga.kind, varga.status, varga.roles) == ("athlete", "linked", ("team:Q8682",))
    # A person with no public role links, and the product labels them unverified.
    assert (moreno.kind, moreno.status, moreno.roles) == ("person", "linked", ())
    assert (freedonia.kind, freedonia.status) == ("government", "linked")
    assert (lumen.kind, lumen.status) == ("company", "linked")
    assert tiny.status == "unclear"
    idx = EntityIndex([freedonia, lumen])
    assert idx.bound(actor("wikidata", "Q204", "Freedonia"), EventType("sanction_or_tariff"))
    assert idx.bound(actor("wikidata", "Q205", "Lumen Labs"), EventType("funding"))


async def test_wikidata_is_read_from_robots_allowed_pages():
    import httpx

    from app.services.registry import HttpResolver

    seen = []
    ent = {"entities": {"Q42": {"labels": {"en": {"value": "Kestrel"}},
                                "claims": {"P31": [{"mainsnak": {"datavalue": {
                                    "value": {"id": "Q6256"}}}}]},
                                "sitelinks": {"enwiki": {}}}}}

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/wiki/Q42":
            return httpx.Response(200, text='<li data-mw-revid="777">')
        if request.url.path == "/wiki/Special:EntityData/Q42.json":
            return httpx.Response(200, json=ent)
        return httpx.Response(404)

    res = HttpResolver(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rec = await res.fetch("wikidata", "q42", NOW)
    assert (rec.name, rec.revision, rec.classes, rec.established) == (
        "Kestrel", "777", ("Q6256",), True)
    assert [p for p, _ in seen] == ["/wiki/Q42", "/wiki/Special:EntityData/Q42.json"]
    assert seen[0][1]["action"] == "history" and seen[1][1] == {"revision": "777"}
    assert not any(p.startswith("/w/") for p, _ in seen)
    assert await res.fetch("wikidata", "Q1/../x", NOW) is None and len(seen) == 2
    assert await res.fetch("wikidata", "Q43", NOW) is None


async def test_registry_kinds_follow_the_classes_seen_on_live_items():
    from app.services.registry import Registry, RegistryRecord as R

    reg = Registry()
    cases = {
        ("Q187939",): "company",
        ("Q7603708", "Q37002670"): "legislature",
        ("Q748720", "Q35798", "Q11204"): "government",
        ("Q20857065", "Q480242"): "statistics_agency",
        ("Q135408445",): "team",
        ("Q15229207",): "award",
        ("Q1068785",): None,
    }
    for classes, kind in cases.items():
        rec = await reg.vet(R("wikidata", "Q1", "X", established=True, classes=classes,
                              sitelinks=20), 0)
        assert (rec.kind, rec.status) == (kind, "linked" if kind else "unclear"), classes
