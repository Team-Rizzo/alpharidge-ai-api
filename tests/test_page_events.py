"""APP-711: one page is one event, a rejected claim leaves no event, and response labels."""

import importlib.util
from pathlib import Path

from app.services import line, product
from app.services.store import MemoryStore
from tests.pipeline import HOUR_MS, T0, Lane

ACTORS = {"seoultech": "SEOULTECH", "full": "Seoul National University of Science and "
          "Technology (SeoulTech)", "long": "Seoul National University of Science and Technology"}


def spelled(name: str) -> dict:
    return dict(event_type="breakthrough", category="technology", stage=None,
                headline=f"{name} develops liver-on-a-chip platform",
                actors=[{"id_scheme": "other", "id": name.lower(), "role": "organisation",
                         "name": name}],
                facts=[{"field": "result", "value": "liver-on-a-chip platform"}],
                quote="Researchers develop a liver-on-a-chip platform.")


async def test_one_page_reported_with_three_spellings_is_one_event():
    lane = Lane(check=True)
    await lane.sources()
    url = "https://newsone.com/liver-chip"
    rows = [await lane.submit(f"m{i}", T0 + i * 1000, source_url=url, **spelled(name))
            for i, name in enumerate(ACTORS.values())]
    await lane.step(T0 + 10_000)
    await lane.step(T0 + 20_000)
    events = {lane.event_of(r) for r in rows}
    assert len(events) == 1
    assert [lane.store.claims[r.id].status for r in rows[1:]] == ["rejected", "rejected"]
    assert {e.id for e in lane.store.events.values()} == events


async def test_a_listing_page_keeps_its_events_apart(monkeypatch):
    lane = Lane(check=True)
    await lane.sources()
    monkeypatch.setattr(lane.verifier.registry, "is_listing", lambda url: True)
    from app.services import novelty

    real = novelty.apply

    async def listed(*a, **kw):
        return await real(*a, **{**kw, "listing": True})

    monkeypatch.setattr(novelty, "apply", listed)
    url = "https://newsone.com/results"
    a = await lane.submit("m1", T0, source_url=url, **spelled("Kestrel Labs"))
    b = await lane.submit("m2", T0 + 1000, source_url=url, **spelled("Orbit Studios"))
    await lane.step(T0 + 10_000)
    assert lane.event_of(a) != lane.event_of(b)


async def test_a_line_whose_claims_were_all_rejected_is_deleted_a_day_on():
    lane = Lane(check=False)
    await lane.sources()
    row = await lane.submit("m1", T0, source_url="https://newsone.com/nothing")
    await lane.step(T0 + 10_000)
    ev = lane.event_of(row)
    assert lane.store.claims[row.id].status == "rejected"
    assert lane.store.events[ev].confidence == "line"
    lane.store.event_added_ms[ev] = T0
    assert await line.drop_dead(lane.store, T0 + HOUR_MS) == 0
    assert await line.drop_dead(lane.store, T0 + (line.DEAD_LINE_H + 1) * HOUR_MS) == 1
    assert ev not in lane.store.events
    assert lane.store.claims[row.id].event_id is None


def test_each_stored_confidence_has_a_response_label():
    assert product.STATUS == {"confirmed": "corroborated", "reported": "single_source",
                              "unverified": "awaiting_source", "line": "pending"}
    assert product.status_of(None) is None


async def test_an_event_summary_carries_its_label():
    lane = Lane(check=True)
    await lane.sources()
    row = await lane.submit("m1", T0, source_url="https://newsone.com/e9")
    await lane.step(T0 + 10_000)
    ev = lane.store.events[lane.event_of(row)]
    s = await product.summary(lane.store, ev, lane.entities)
    assert (s["confidence"], s["status"]) == ("reported", "single_source")


def _merge_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "merge_page_events.py"
    spec = importlib.util.spec_from_file_location("merge_page_events", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_cleanup_keeps_the_earliest_event_of_each_page():
    merges = _merge_script().merges
    assert merges([[5, 3], [3, 9], [20, 21]]) == {3: [5, 9], 20: [21]}
    assert merges([[7]]) == {}
