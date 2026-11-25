"""Re-check stored award, injury, match_result and transfer events against the judge's sport
rule (prompt v18: the subject is an athlete, coach, team or club in a sport) and hide the ones
that are not sport.

    python scripts/recheck_sport_types.py                  # dry run over the last 30 days
    python scripts/recheck_sport_types.py --days 7 --apply # hide what the dry run lists
    python scripts/recheck_sport_types.py --restore        # count the events it hid
    python scripts/recheck_sport_types.py --restore --apply  # un-hide them

Each event's lead claim is asked, on its quote and source URL (no page fetch), the v18 judge's
own kind question (the types' v18 wording) and a sport question in the words of the v18 sport
rule. An event is hidden only when both score sport below JUDGE_NO; an unclear answer, an error
or a missing claim changes nothing.

It touches events only, never claims, ledger rows or pay. A hidden event gets hold and (when
unmarked) marked set to "off_type": it leaves the product list and feed, a "marked" feed record
tells readers who already have it, and --restore reverses both. Nothing is deleted.

Every run writes a CSV (--csv, default recheck_sport_<time>.csv) with one row per event. The
judge key is EL_JUDGE_API_KEY (or OPENROUTER_API_KEY); URL and model are EL_JUDGE_URL and
EL_JUDGE_MODEL, defaulting to the validators'. DATABASE_URL comes from the environment or the
repo's .env, as for the API.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  (loads .env)
import httpx  # noqa: E402

from alpharidge_ai.judge import prompt  # noqa: E402
from alpharidge_ai.schema import Claim  # noqa: E402
from app.services import product  # noqa: E402
from app.services.store import WON, FeedRow, Store  # noqa: E402

SPORT_TYPES = ("award", "injury", "match_result", "transfer")
MIN_PROMPT = 18
OFF_TYPE = product.OFF_TYPE
SPORT, NOT_SPORT, UNCLEAR, SKIP = "sport", "not_sport", "unclear", "skip"
DEFAULT_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.14"
DAY_MS = 86_400_000
COLUMNS = ("event_id", "event_type", "headline", "first_seen", "confidence", "claim_id", "url",
           "quote", "p_sport", "p_kind_sport", "kind_pick", "verdict", "action", "hold_was",
           "marked_was", "note")


def prompt_ok(version: str = prompt.JUDGE_PROMPT_VERSION) -> bool:
    try:
        return int(version.lstrip("v")) >= MIN_PROMPT
    except ValueError:
        return False


SPORT_Q = {
    "sport": "Yes: an athlete, coach, team, club, league or sports body, in news about their "
             "sport (a match, a move, an injury, an award, an appointment or other sports news).",
    "other": "No: the subject or the news is outside sport, such as music, film, theatre, TV, "
             "books, journalism, business, markets, science, education, charity, politics or "
             "public service, or people hurt in a road accident, an attack, a fall, a fire or a "
             "disaster."}


def questions(claim) -> dict:
    """The v18 judge's kind question for this claim, as the validators ask it, and the sport
    question."""
    q = prompt.judge_questions(claim)[0]
    subject = prompt.values(claim)["subject"]
    sport = {"type": "choice", "criteria": SPORT_Q, "instructions": (
        f"In the marked passage, is {subject} an athlete, coach, team or club in a sport, and "
        f"is the news about their sport?")}
    return {"kind": q["kind"], "sport": sport}


def state(claim, url: str, published: Optional[str]) -> list:
    quote = prompt.dated(claim.quote or "", published)
    when = prompt._clean(published or "unknown")
    return [f"Passage from {prompt._clean(url)}, published {when}:",
            "[[" + prompt._cut(quote) + "]]"]


def _probs(answers: dict, name: str) -> dict:
    a = answers.get(name)
    p = a.get("probabilities") if isinstance(a, dict) else None
    return {k: v for k, v in (p or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def classify(answers: dict) -> tuple:
    """(verdict, p_sport, p_kind_sport, kind_pick) from the judge's answers. Not sport only when
    the sport question and every sports type of the kind question together score below
    JUDGE_NO; sport when the sport question scores JUDGE_YES; else unclear."""
    sport, kinds = _probs(answers, "sport"), _probs(answers, "kind")
    p = sport.get("sport") if sport else None
    k = round(sum(v for n, v in kinds.items() if n.split(":")[0] in SPORT_TYPES), 4) if kinds \
        else None
    pick = (answers.get("kind") or {}).get("choice") if isinstance(answers.get("kind"), dict) \
        else None
    if p is None:
        return UNCLEAR, None, k, pick
    if p < prompt.JUDGE_NO and k is not None and k < prompt.JUDGE_NO:
        return NOT_SPORT, p, k, pick
    return (SPORT if p >= prompt.JUDGE_YES else UNCLEAR), p, k, pick


class Decider:
    """Posts one decision request per claim; keeps the reported cost."""

    def __init__(self, api_key: str, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                 timeout_s: float = 60, transport: Optional[httpx.BaseTransport] = None):
        self.model = model
        self.url = url
        self.client = httpx.Client(timeout=timeout_s, transport=transport, headers={
            "authorization": f"Bearer {api_key}", "content-type": "application/json"})
        self.cost = 0.0
        self.calls = 0

    def ask(self, claim, url: str, published: Optional[str]) -> dict:
        body = {"model": self.model, "state": state(claim, url, published),
                "questions": questions(claim)}
        last = None
        for _ in range(3):
            try:
                r = self.client.post(self.url, json=body)
                r.raise_for_status()
                out = r.json()
                break
            except (httpx.HTTPError, ValueError) as e:
                last = e
        else:
            raise RuntimeError(f"judge call failed: {type(last).__name__}")
        self.calls += 1
        cost = (out.get("usage") or {}).get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            self.cost += float(cost)
        answers = out.get("answers")
        return answers if isinstance(answers, dict) else {}


@dataclass
class Row:
    event_id: int
    event_type: str
    headline: str = ""
    first_seen: str = ""
    confidence: str = ""
    claim_id: str = ""
    url: str = ""
    quote: str = ""
    p_sport: Optional[float] = None
    p_kind_sport: Optional[float] = None
    kind_pick: Optional[str] = None
    verdict: str = SKIP
    action: str = "none"
    hold_was: Optional[str] = None
    marked_was: Optional[str] = None
    note: str = ""

    def csv(self) -> list:
        return ["" if getattr(self, c) is None else getattr(self, c) for c in COLUMNS]


def _iso(ms: Optional[int]) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else ""


async def candidates(store: Store, since_ms: int) -> List[int]:
    ids = {c.event_id for c in await store.claims_of_types(since_ms, SPORT_TYPES)
           if c.event_id is not None}
    return sorted(ids)


async def _lead(store: Store, ev):
    return await product._lead(store, ev, await store.claims_for_event(ev.id, WON))


async def _feed_started(store: Store, event_id: int) -> bool:
    return any(r.kind == product.EVENT for r in await store.feed(event_id=event_id, limit=1000))


async def _mark(store: Store, ev, marked, now_ms: int) -> None:
    if await _feed_started(store, ev.id):
        await store.add_feed(FeedRow(kind=product.MARKED, event_id=ev.id, category=ev.category,
                                     event_type=ev.event_type, at_ms=now_ms,
                                     payload={"marked": marked}))


async def hide(store: Store, ev, now_ms: int) -> None:
    fields = {"hold": OFF_TYPE}
    if ev.marked is None:
        fields["marked"] = OFF_TYPE
    async with store.transaction() as tx:
        await tx.update_event(ev.id, **fields)
        if "marked" in fields:
            await _mark(tx, ev, OFF_TYPE, now_ms)


async def unhide(store: Store, ev, now_ms: int) -> None:
    fields = {"hold": None}
    if ev.marked == OFF_TYPE:
        fields["marked"] = None
    async with store.transaction() as tx:
        await tx.update_event(ev.id, **fields)
        if "marked" in fields:
            await _mark(tx, ev, None, now_ms)


async def check_event(store: Store, decider: Decider, event_id: int) -> Row:
    ev = await store.get_event(event_id)
    if ev is None:
        return Row(event_id, "", note="no event")
    row = Row(ev.id, ev.event_type, ev.headline, _iso(ev.first_seen_ms), ev.confidence,
              hold_was=ev.hold, marked_was=ev.marked)
    if ev.event_type not in SPORT_TYPES:
        row.note = "event is another type"
        return row
    if ev.confidence == product.LINE:
        row.note = "open line"
        return row
    if ev.hold is not None:
        row.note = "already held"
        return row
    lead = await _lead(store, ev)
    if lead is None:
        row.note = "no claim"
        return row
    try:
        claim = Claim.model_validate(lead.payload)
    except ValueError:
        row.note = "claim does not parse"
        return row
    fetches = [f for f in await store.source_fetches(lead.id) if f.published_ms]
    published = _iso(fetches[-1].published_ms)[:10] if fetches else None
    url = (fetches[-1].final_url if fetches and fetches[-1].final_url else lead.source_url)
    row.claim_id, row.url, row.quote = str(lead.id), url, (claim.quote or "")[:300]
    try:
        answers = await asyncio.to_thread(decider.ask, claim, url, published)
    except RuntimeError as e:
        row.verdict, row.note = UNCLEAR, str(e)
        return row
    row.verdict, row.p_sport, row.p_kind_sport, row.kind_pick = classify(answers)
    return row


async def recheck(store: Store, decider: Decider, since_ms: int, apply: bool, now_ms: int,
                  workers: int = 4, max_usd: float = 1.0,
                  log=print) -> List[Row]:
    ids = await candidates(store, since_ms)
    log(f"{len(ids)} events with claims of {', '.join(SPORT_TYPES)} since {_iso(since_ms)}")
    sem = asyncio.Semaphore(max(1, workers))
    rows: List[Row] = []

    async def one(event_id: int) -> None:
        async with sem:
            if decider.cost >= max_usd:
                rows.append(Row(event_id, "", verdict=SKIP, note="budget reached"))
                return
            row = await check_event(store, decider, event_id)
            if row.verdict == NOT_SPORT:
                if apply:
                    ev = await store.get_event(event_id)
                    if ev is not None and ev.hold is None:
                        await hide(store, ev, now_ms)
                        row.action = "hidden"
                else:
                    row.action = "would_hide"
            rows.append(row)

    await asyncio.gather(*(one(i) for i in ids))
    rows.sort(key=lambda r: r.event_id)
    return rows


async def restore(store: Store, event_ids: Iterable[int], now_ms: int) -> List[Row]:
    rows = []
    for event_id in sorted(set(event_ids)):
        ev = await store.get_event(event_id)
        if ev is None or ev.hold != OFF_TYPE:
            continue
        row = Row(ev.id, ev.event_type, ev.headline, _iso(ev.first_seen_ms), ev.confidence,
                  hold_was=ev.hold, marked_was=ev.marked, verdict=SKIP, action="restored")
        await unhide(store, ev, now_ms)
        rows.append(row)
    return rows


def write_csv(path: str, rows: Iterable[Row]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow(r.csv())


def summary(rows: List[Row]) -> str:
    counts: dict = {}
    for r in rows:
        counts[(r.event_type, r.verdict, r.action)] = counts.get(
            (r.event_type, r.verdict, r.action), 0) + 1
    return "\n".join(f"  {t or '-':13} {v:9} {a:10} {n}" for (t, v, a), n in sorted(counts.items()))


async def _held_ids(db) -> List[int]:
    rows = await db.query_raw("SELECT id FROM events WHERE hold = $1 ORDER BY id", OFF_TYPE)
    return [int(r["id"]) for r in rows]


async def main_async(args) -> int:
    from prisma import Prisma

    from app.services.store import PrismaStore

    url = os.getenv("DATABASE_URL")
    db = Prisma(http={"timeout": 600}, **({"datasource": {"url": url}} if url else {}))
    await db.connect()
    now_ms = time.time_ns() // 1_000_000
    try:
        store = PrismaStore(db)
        if args.restore:
            ids = await _held_ids(db)
            if not args.apply:
                print(f"dry run: {len(ids)} events held {OFF_TYPE}; --apply un-hides them")
                return 0
            rows = await restore(store, ids, now_ms)
        else:
            key = (os.getenv("EL_JUDGE_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "").strip()
            if not key:
                print("set EL_JUDGE_API_KEY (or OPENROUTER_API_KEY)")
                return 2
            decider = Decider(key, os.getenv("EL_JUDGE_URL") or DEFAULT_URL,
                              os.getenv("EL_JUDGE_MODEL") or DEFAULT_MODEL)
            rows = await recheck(store, decider, now_ms - args.days * DAY_MS, args.apply,
                                 now_ms, args.workers, args.max_usd)
            print(f"{decider.calls} judge calls, ${decider.cost:.4f}")
    finally:
        await db.disconnect()
    write_csv(args.csv, rows)
    print(summary(rows))
    print(f"{'applied' if args.apply else 'dry run'}; report: {args.csv}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="hide (or with --restore, un-hide)")
    p.add_argument("--days", type=float, default=30, help="claims received in the last N days")
    p.add_argument("--csv", default=f"recheck_sport_{time.strftime('%Y%m%dT%H%M%S')}.csv")
    p.add_argument("--workers", type=int, default=4, help="judge calls at once")
    p.add_argument("--max-usd", type=float, default=1.0, help="stop asking past this spend")
    p.add_argument("--restore", action="store_true", help=f"un-hide every {OFF_TYPE} event")
    args = p.parse_args()
    if args.days <= 0:
        p.error("--days must be positive")
    if not args.restore and not prompt_ok():
        p.error(f"the installed judge prompt is {prompt.JUDGE_PROMPT_VERSION}; this needs "
                f"v{MIN_PROMPT} or later (the sport rule)")
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
