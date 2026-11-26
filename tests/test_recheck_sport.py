"""scripts/recheck_sport_types.py: hides non-sport award, injury, result and transfer events,
touching events only."""

import csv
import importlib.util
import json
import os
import sys
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path

import httpx
import pytest

from alpharidge_ai.schema import Claim
from app.services import product
from app.services.store import EventQuery, EventRecord, FeedRow
from tests.pipeline import T0, Lane

_path = Path(__file__).resolve().parent.parent / "scripts" / "recheck_sport_types.py"
_spec = importlib.util.spec_from_file_location("recheck_sport_types", _path)
rs = importlib.util.module_from_spec(_spec)
sys.modules["recheck_sport_types"] = rs
_spec.loader.exec_module(rs)

NOW = T0 + 30 * 86_400_000
SPORTS = {"kind": {"type": "choice", "choice": "award",
                   "probabilities": {"award": 0.8, "match_result": 0.1, "none": 0.1}},
          "sport": {"type": "choice", "choice": "sport",
                    "probabilities": {"sport": 0.97, "other": 0.03}}}
OTHER = {"kind": {"type": "choice", "choice": "none",
                  "probabilities": {"none": 0.9, "award": 0.1}},
         "sport": {"type": "choice", "choice": "other",
                   "probabilities": {"sport": 0.02, "other": 0.98}}}


def _answers(kind_sport, sport):
    return {"kind": {"type": "choice", "probabilities": {"award": kind_sport,
                                                         "none": 1 - kind_sport}},
            "sport": {"type": "choice", "probabilities": {"sport": sport, "other": 1 - sport}}}


def test_not_sport_only_when_both_questions_agree():
    assert rs.classify(OTHER)[0] == rs.NOT_SPORT
    assert rs.classify(SPORTS)[0] == rs.SPORT
    assert rs.classify(_answers(0.1, 0.9))[0] == rs.SPORT
    assert rs.classify(_answers(0.9, 0.1))[0] == rs.UNCLEAR
    assert rs.classify(_answers(0.1, 0.5))[0] == rs.UNCLEAR
    assert rs.classify({})[0] == rs.UNCLEAR
    assert rs.classify({"sport": OTHER["sport"]})[0] == rs.UNCLEAR
    stage = {"kind": {"probabilities": {"transfer:completed": 0.3, "injury": 0.3}},
             "sport": OTHER["sport"]}
    assert rs.classify(stage)[:3] == (rs.UNCLEAR, 0.02, 0.6)


def test_it_needs_the_v18_prompt():
    assert not rs.prompt_ok("v17") and rs.prompt_ok("v18") and rs.prompt_ok("v19")
    assert not rs.prompt_ok("x")


def _claim(**kw) -> Claim:
    base = dict(kind="event", headline="Ana Lee: award", event_type="award", category="sports",
                event_date="2026-10-13", source_url="https://newsone.com/a",
                quote="Ana Lee won the MVP award.",
                actors=[{"id_scheme": "other", "id": "Ana Lee", "role": "recipient",
                         "name": "Ana Lee"}],
                facts=[{"field": "award", "value": "MVP"}])
    base.update(kw)
    return Claim(**base)


def test_the_questions_are_the_judges_kind_question_and_the_sport_question():
    from alpharidge_ai.judge import prompt
    claim = _claim()
    q = rs.questions(claim)
    judged = prompt.judge_questions(claim)[0]
    assert q["kind"] == judged["kind"] and set(q) == {"kind", "sport"}
    assert set(q["sport"]["criteria"]) == {"sport", "other"} and "Ana Lee" in q["sport"][
        "instructions"]
    st = rs.state(claim, "https://newsone.com/a", "2026-10-13")
    assert st[1] == "[[Ana Lee won the MVP award.]]" and "newsone.com/a" in st[0]


def test_the_decider_posts_one_request_and_keeps_the_cost():
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"answers": OTHER, "usage": {"cost": 0.0001}})

    waits = []
    d = rs.Decider("k", "https://judge.test/x", "m", transport=httpx.MockTransport(handle),
                   sleep=waits.append)
    assert d.ask(_claim(), "https://newsone.com/a", None) == OTHER
    assert len(sent) == 2 and sent[1]["model"] == "m"
    assert set(sent[1]["questions"]) == {"kind", "sport"}
    assert d.calls == 1 and d.cost == pytest.approx(0.0001)

    assert waits == [rs.RETRY_WAITS[0]]

    tries = []

    def busy(request):
        tries.append(1)
        return httpx.Response(429, headers={"retry-after": "40"})

    down = rs.Decider("k", "https://judge.test/x", "m", transport=httpx.MockTransport(busy),
                      sleep=waits.append)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        down.ask(_claim(), "https://newsone.com/a", None)
    assert len(tries) == len(rs.RETRY_WAITS) + 1
    assert waits[1:] == [max(w, 40) for w in rs.RETRY_WAITS]

    tries.clear()
    bad = rs.Decider("k", "https://judge.test/x", "m", sleep=waits.append,
                     transport=httpx.MockTransport(lambda r: tries.append(1) or httpx.Response(401)))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        bad.ask(_claim(), "https://newsone.com/a", None)
    assert len(tries) == 1


class FakeDecider:
    def __init__(self, by_url):
        self.by_url, self.cost, self.calls = by_url, 0.0, 0

    def ask(self, claim, url, published):
        self.calls += 1
        if self.by_url.get(url) is None:
            raise RuntimeError("judge call failed: HTTPError")
        return self.by_url[url]


def _sport_claim(url, name, event_type="award"):
    actors = [{"id_scheme": "other", "id": name, "role": "recipient", "name": name}]
    facts = [{"field": "award", "value": "Best Album"}]
    if event_type != "award":
        actors = [{"id_scheme": "other", "id": name, "role": "player", "name": name}]
        facts = [{"field": "to_team", "value": "Reds"}] if event_type == "transfer" else []
    return dict(source_url=url, event_type=event_type, category="sports",
                headline=f"{name}: {event_type}", actors=actors, facts=facts,
                quote=f"{name} was named in the news.")


async def _lane():
    lane = Lane(check=True)
    await lane.sources()
    seen = T0 + 61_000
    ids = {}
    for url, name, t in (("https://newsone.com/music", "Kay Singer", "award"),
                         ("https://wiretwo.org/move", "Ana Lee", "transfer"),
                         ("https://dailythree.net/down", "Bo Ray", "injury")):
        lane.page(url, text=f"filler text here. {name} was named in the news.")
        row = await lane.submit("m1", T0 + 60_000, **_sport_claim(url, name, t))
        await lane.step(seen)
        ids[url] = lane.event_of(row)
    assert len(set(ids.values())) == 3 and None not in ids.values()
    for eid in ids.values():
        await product.sync_event(lane.store, eid, seen, lane.entities)
    return lane, ids


def _snapshot(lane):
    s = lane.store
    return (json.dumps([asdict(r) for r in s.ledger], default=str, sort_keys=True),
            json.dumps({k: asdict(c) for k, c in s.claims.items()}, default=str, sort_keys=True))


async def _listed(lane):
    q = EventQuery(published_by_ms=NOW, confidences=("reported", "confirmed", "unverified"),
                   limit=100)
    return {e.id for e in await lane.store.list_events(q)}


async def test_dry_run_changes_nothing_and_apply_hides_only_the_non_sport_event(tmp_path):
    lane, ids = await _lane()
    music, move, down = (ids[u] for u in ("https://newsone.com/music",
                                          "https://wiretwo.org/move",
                                          "https://dailythree.net/down"))
    decider = FakeDecider({"https://newsone.com/music": OTHER,
                           "https://wiretwo.org/move": SPORTS})
    before, feed_before = _snapshot(lane), len(lane.store.feed_rows)
    assert await _listed(lane) == {music, move, down}

    rows = await rs.recheck(lane.store, decider, T0, False, NOW, log=lambda *_: None)
    by_id = {r.event_id: r for r in rows}
    assert (by_id[music].verdict, by_id[music].action) == (rs.NOT_SPORT, "would_hide")
    assert (by_id[move].verdict, by_id[move].action) == (rs.SPORT, "none")
    assert (by_id[down].verdict, by_id[down].action) == (rs.FAILED, "none")
    assert _snapshot(lane) == before and len(lane.store.feed_rows) == feed_before
    assert lane.store.events[music].hold is None

    report = tmp_path / "r.csv"
    rs.write_csv(str(report), rows)
    got = list(csv.DictReader(open(report)))
    assert [r["event_id"] for r in got] == [str(i) for i in sorted(ids.values())]
    assert set(got[0]) == set(rs.COLUMNS)

    # Unverified events never reach the feed; this one stands for one that did.
    assert lane.store.events[music].confidence == product.UNVERIFIED
    await lane.store.add_feed(FeedRow(kind=product.EVENT, event_id=music, category="sports",
                                      event_type="award", at_ms=NOW, payload={}))
    feed_before += 1
    rows = await rs.recheck(lane.store, decider, T0, True, NOW, log=lambda *_: None)
    assert {r.event_id: r.action for r in rows}[music] == "hidden"
    ev = lane.store.events[music]
    assert (ev.hold, ev.marked) == (rs.OFF_TYPE, rs.OFF_TYPE)
    assert _snapshot(lane) == before
    assert await _listed(lane) == {move, down}
    assert not product.visible(ev, NOW)
    (mark,) = lane.store.feed_rows[feed_before:]
    assert (mark.kind, mark.event_id, mark.payload) == (product.MARKED, music,
                                                        {"marked": rs.OFF_TYPE})

    # A later sync keeps the hold; a second run leaves the held event alone.
    await product.sync_event(lane.store, music, NOW, lane.entities)
    assert lane.store.events[music].hold == rs.OFF_TYPE
    again = await rs.recheck(lane.store, decider, T0, True, NOW, log=lambda *_: None)
    assert {r.event_id: r.note for r in again}[music] == "already held"
    assert len(lane.store.feed_rows) == feed_before + 1

    restored = await rs.restore(lane.store, [music, move], NOW)
    assert [r.event_id for r in restored] == [music]
    ev = lane.store.events[music]
    assert (ev.hold, ev.marked) == (None, None)
    assert await _listed(lane) == {music, move, down}
    assert lane.store.feed_rows[-1].payload == {"marked": None}
    assert _snapshot(lane) == before


async def test_a_mark_already_on_the_event_is_kept(tmp_path):
    lane, ids = await _lane()
    music = ids["https://newsone.com/music"]
    await lane.store.update_event(music, marked="retracted")
    feed_before = len(lane.store.feed_rows)
    await rs.recheck(lane.store, FakeDecider({"https://newsone.com/music": OTHER}), T0, True,
                     NOW, log=lambda *_: None)
    ev = lane.store.events[music]
    assert (ev.hold, ev.marked) == (rs.OFF_TYPE, "retracted")
    assert len(lane.store.feed_rows) == feed_before
    await rs.restore(lane.store, [music], NOW)
    assert (lane.store.events[music].hold, lane.store.events[music].marked) == (None, "retracted")


async def test_the_budget_stops_further_calls():
    lane, _ = await _lane()
    decider = FakeDecider({"https://newsone.com/music": OTHER})
    decider.cost = 5.0
    rows = await rs.recheck(lane.store, decider, T0, True, NOW, max_usd=1.0,
                            log=lambda *_: None)
    assert decider.calls == 0 and {r.note for r in rows} == {"budget reached"}
    assert all(e.hold is None for e in lane.store.events.values())


@pytest.fixture(params=["memory", "prisma"])
async def any_store(request):
    from app.services.store import MemoryStore
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


async def test_hide_and_unhide_in_either_store(any_store):
    store = any_store
    ev = await store.add_event(EventRecord("award", "sports", date(2026, 10, 13),
                                           "fp-" + uuid.uuid4().hex, confidence="reported"))
    await store.update_event(ev.id, publish_ms=T0, first_seen_ms=T0)
    await store.add_feed(FeedRow(kind=product.EVENT, event_id=ev.id, category="sports",
                                 event_type="award", at_ms=T0, payload={}))
    q = EventQuery(published_by_ms=NOW, confidences=("reported",), event_type="award",
                   since_ms=T0, until_ms=T0 + 1, limit=1000)
    assert ev.id in {e.id for e in await store.list_events(q)}
    await rs.hide(store, await store.get_event(ev.id), NOW)
    got = await store.get_event(ev.id)
    assert (got.hold, got.marked) == (rs.OFF_TYPE, rs.OFF_TYPE)
    assert ev.id not in {e.id for e in await store.list_events(q)}
    await rs.unhide(store, got, NOW + 1)
    got = await store.get_event(ev.id)
    assert (got.hold, got.marked) == (None, None)
    assert ev.id in {e.id for e in await store.list_events(q)}
    feed = await store.feed(event_id=ev.id, limit=10)
    assert [(r.kind, r.payload) for r in feed] == [
        (product.EVENT, {}), (product.MARKED, {"marked": rs.OFF_TYPE}),
        (product.MARKED, {"marked": None})]
