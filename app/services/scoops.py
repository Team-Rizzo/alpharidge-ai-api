"""Open scoops, for validators to look for on the trusted list, and the pages they find."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from alpharidge_ai.schema import Actor, Claim, IdScheme, confirm_claim, judged, renamed
from alpharidge_ai.verify.check import PIVOT_SLOTS, pick_quote
from app import ROOT
from app.services import intake, judge as judges, ledger, novelty, trust
from app.services.entities import LINKED, SEC_NAMES
from app.services.store import ClaimRow, Job, ScoopCandidateRow, Store
from app.utils.clock import HOUR_MS, iso, ms as _ms, now_ms as _now_ms

CONFIRM_JOB = "scoop_confirm"
FEEDS_PATH = os.getenv("EL_SCAN_FEEDS") or ROOT / "app" / "data" / "scan_feeds.json"

QUEUED, CONFIRMED, REJECTED, CLOSED = "queued", "confirmed", "rejected", "closed"
# Who found a page that a trusted claim on the event cites.
API_FINDER = "api"

logger = logging.getLogger(__name__)


def closes_ms(row: ClaimRow) -> int:
    return row.received_ms + int(novelty.SCOOP_WINDOW_H * HOUR_MS)


def is_open(row: Optional[ClaimRow], now_ms: int) -> bool:
    return (row is not None and row.route == "scoop" and row.status == "verified"
            and now_ms < closes_ms(row))


@lru_cache(maxsize=1)
def feeds() -> Tuple[dict, ...]:
    try:
        with open(FEEDS_PATH, encoding="utf-8") as f:
            rows = json.load(f)
        rows = rows.get("feeds", []) if isinstance(rows, dict) else rows
    except (OSError, ValueError, AttributeError) as e:
        logger.warning(f"No scan feeds: {e!r}")
        return ()
    return tuple({"url": r["url"], "kind": r.get("kind", "rss")} for r in rows
                 if isinstance(r, dict) and isinstance(r.get("url"), str))


def since(claim: Claim) -> datetime:
    """Trusted pages about the event are looked for from this time on."""
    day = claim.event_date - timedelta(days=novelty.DATE_DRIFT_D)
    return datetime.combine(day, time(), timezone.utc)


def _forms(actor: Actor, event_type, entities) -> Tuple[List[str], List[str]]:
    """Other names and the tickers of one actor's entity: the registry's for its link or ID,
    the SEC's for a CIK, and the filer's name for a ticker given as its name."""
    names, tickers = [], []
    link = entities.link(actor, event_type)
    rec = link.entity if link is not None else (
        entities.get(actor.id_scheme.value, actor.id)
        if actor.id_scheme is not IdScheme.OTHER else None)
    if rec is not None and rec.status == LINKED:
        names += [rec.name, *rec.aliases]
        tickers += [x.split(":", 1)[1] for x in rec.xrefs if x.startswith("ticker:")]
    if actor.id_scheme is IdScheme.CIK and actor.id.strip().isdigit():
        names.append(SEC_NAMES.names.get(actor.id.strip().zfill(10), ""))
        tickers += SEC_NAMES.tickers_of(actor.id.strip())
    filer = SEC_NAMES.name_of_ticker(actor.name)
    if filer:
        names.append(filer)
        tickers.append(actor.name.strip())
    return [n for n in names if n], [t for t in tickers if t]


def forms(claim: Claim, entities) -> Dict[str, List[str]]:
    """Each actor's name, and the type's subject when it is a fact, with the other forms of
    its entity, for a confirming page that writes it another way (`schema.renamed`)."""
    out: Dict[str, List[str]] = {}
    for a in claim.actors:
        names, tickers = _forms(a, claim.event_type, entities)
        out.setdefault(a.name or a.id, []).extend(names + tickers)
    pivot = PIVOT_SLOTS.get(claim.event_type.value)
    for f in claim.facts:
        if f.field == pivot and isinstance(f.value, str):
            names, tickers = _forms(Actor(id_scheme=IdScheme.OTHER, id=f.value[:64],
                                          name=f.value), claim.event_type, entities)
            out.setdefault(f.value, []).extend(names + tickers)
    return {k: list(dict.fromkeys(v)) for k, v in out.items() if v}


def subjects(claim: Claim, entities) -> Tuple[List[str], List[str]]:
    """The names and tickers validators look for: the subject's names, every actor's name and
    the other names and tickers of their entities (`_forms`)."""
    names, tickers = novelty.subject_names(claim, entities), []
    for a in claim.actors:
        names.append(a.name or a.id)
        more, marks = _forms(a, claim.event_type, entities)
        names += more
        tickers += marks
    return ([n for n in dict.fromkeys(names) if n],
            [t for t in dict.fromkeys(tickers) if t])


def item(row: ClaimRow, entities) -> dict:
    """What validators get to look for a scoop: never its page, source, quote or hotkey."""
    claim = Claim.model_validate(row.payload)
    names, tickers = subjects(claim, entities)
    return {
        "id": row.id, "event_type": claim.event_type.value,
        "stage": claim.stage.value if claim.stage else None,
        "event_date": claim.event_date.isoformat(), "received_at": iso(row.received_ms),
        "since": since(claim).isoformat().replace("+00:00", "Z"),
        "closes_at": iso(closes_ms(row)),
        "subjects": names, "tickers": tickers,
        "actor_ids": [{"id_scheme": a.id_scheme.value, "id": a.id} for a in claim.actors
                      if a.id_scheme is not IdScheme.OTHER],
        "facts": [{"field": f.field, "value": f.value, "unit": f.unit} for f in claim.facts],
    }


async def open_scoops(store: Store, entities, now_ms: int) -> List[dict]:
    """Held scoops still open, leaving out those on an event another scoop has taken."""
    out, taken = [], {}
    for r in await store.claims_by_status("verified", limit=100_000):
        if not is_open(r, now_ms):
            continue
        if r.event_id is not None and r.event_id not in taken:
            ev = await store.get_event(r.event_id)
            taken[r.event_id] = ev.first_claim_id if ev is not None else None
        if taken.get(r.event_id) in (None, r.id):
            out.append(item(r, entities))
    return out


async def submit(store: Store, scoop_id: int, url: str, validator: str,
                 now_ms: int) -> Optional[str]:
    """A found page for a scoop: queued, or why not; None when there is no such scoop."""
    row = await store.get_claim(scoop_id)
    if row is None or row.route != "scoop":
        return None
    if row.status in ("provisional", "settled"):
        return "already_confirmed"
    if not is_open(row, now_ms):
        return "closed"
    if await store.add_scoop_candidate(ScoopCandidateRow(scoop_id, url, validator,
                                                         now_ms)) is None:
        return "duplicate"
    await store.add_job(CONFIRM_JOB, scoop_id, now_ms)
    return "queued"


def contradicted(record: dict) -> bool:
    """The judge found the event but read another value for a figure the page prints."""
    answers, fields = record.get("answers") or {}, record.get("fields") or {}
    return (record.get("outcome") == judges.REJECT and "no" in fields.values()
            and all(v == "yes" for v in answers.values()))


def misdated(record: dict) -> bool:
    """The judge found the event and its subject but dates it earlier than the claim does."""
    answers = record.get("answers") or {}
    return (record.get("outcome") == judges.REJECT and record.get("reason") != "fixed_check"
            and record.get("not_news_reason") == "old_event" and answers.get("timing") == "no"
            and answers.get("event") == answers.get("subject") == "yes")


async def contested(store: Store, scoop: ClaimRow, claim: Claim, entities,
                    now_ms: int) -> List[str]:
    """Other held scoops, from any coldkey, received by now while this one was held, that
    name a conflicting outcome for the same subject."""
    window = int(novelty.SCOOP_WINDOW_H * HOUR_MS)
    out = []
    types = ({scoop.event_type, *novelty.PEOPLE_SLOTS}
             if scoop.event_type in novelty.PEOPLE_SLOTS else {scoop.event_type})
    for c in await store.claims_of_types(scoop.received_ms - window, sorted(types)):
        if (c.id == scoop.id or c.route != "scoop" or c.verified_ms is None
                or abs(c.received_ms - scoop.received_ms) >= window or c.received_ms > now_ms):
            continue
        if novelty.conflicting(claim, Claim.model_validate(c.payload), entities):
            out.append(str(c.id))
    return sorted(out, key=int)


class Wait(Exception):
    """The judge cannot answer now; the candidate is tried again later."""


async def _verdict(v, store: Store, scoop: ClaimRow, claim: Claim, page, now_ms: int,
                   task: dict) -> dict:
    out = await v.page_verdict(store, scoop, claim, page, now_ms, task, by_hotkey=False)
    if out is None:
        raise Wait()
    return out


async def check_one(store: Store, scoop: ClaimRow, cand: ScoopCandidateRow, v, resolver,
                    now_ms: int, head: Optional[int] = None) -> str:
    """One found page against a held scoop. Returns the candidate's outcome."""
    ev = await store.get_event(scoop.event_id) if scoop.event_id is not None else None
    if ev is not None and ev.first_claim_id not in (None, scoop.id):
        return "event_taken"
    page = await v.fetcher.fetch(cand.url)
    if not page.ok:
        return page.reason or "fetch_failed"
    url = page.final_url or cand.url
    own = ledger.judge_record(await store.source_fetches(scoop.id)) or {}
    claim = judged(Claim.model_validate_json(scoop.body), own) or Claim.model_validate(
        scoop.payload)
    names = forms(claim, resolver.index())
    try:
        shown = renamed(claim, page.text, names)
        quote = pick_quote(shown, page.text)
        if quote is None:
            return "no_quote"
        found = confirm_claim(claim, cand.url, quote, page.text, names)
    except ValueError:
        return "invalid_schema"
    # The forms used, so validators build the same confirm claim.
    pairs = [(a.name or a.id, b.name) for a, b in zip(claim.actors, shown.actors)]
    pairs += [(f.value, g.value) for f, g in zip(claim.facts, shown.facts)
              if isinstance(f.value, str)]
    written = {k: v for k, v in pairs if v and v != k}
    source = await trust.resolve(store, url, now_ms)
    theirs = await store.get_source(scoop.source_id) if scoop.source_id is not None else None
    ct = await trust.of_claim(store, v.registry, resolver.index(), found, source,
                              scoop.category, url)
    if ct.t < trust.PRIZE_TRUST_MIN:
        return "low_trust"
    if theirs is None or source.owner_id == theirs.owner_id or not (
            ct.primary or await trust.counts_as_owner(store, source, scoop.category)):
        return "same_owner"
    at = datetime.fromtimestamp(cand.found_ms / 1000, tz=timezone.utc)
    result = v.check(found, page, at)
    if not result.passed:
        return result.reason or "unsupported"
    task = judges.task_of(scoop, judges.CONFIRM, cand.url, cand.found_ms,
                          final_url=page.final_url or None, quote=quote,
                          names=written or None,
                          retyped=(own.get("retyped") or {}).get("kind"))
    record = await _verdict(v, store, scoop, found, page, now_ms, task)
    if contradicted(record) or misdated(record):
        await resolver.scoop_failed(store, scoop, "scoop_mismatch", source, now_ms, head)
        return "scoop_mismatch"
    if record.get("outcome") != judges.PASS:
        return record.get("reason") or f"judge_{record.get('outcome')}"
    published = _ms(page.published_at)
    if record.get("retyped"):
        other = judged(found, record)
        if other is not None and novelty.decided_otherwise(claim, other, resolver.index()):
            await resolver.scoop_failed(store, scoop, "scoop_mismatch", source, now_ms, head,
                                        firm=ct.official)
            return "scoop_mismatch"
        return "retyped"
    if published is None:
        return "undated"
    rivals = await contested(store, scoop, claim, resolver.index(), now_ms)
    confirm = {"url": cand.url, "final_url": page.final_url or None,
               "content_hash": page.content_hash, "published_at": iso(published),
               "quote": quote, "claim": found.model_dump(mode="json", exclude_none=True),
               "trust": ct.t, "judge": record, "by": cand.validator,
               "found_at": iso(cand.found_ms), **({"contested": rivals} if rivals else {})}
    await resolver.confirm_scoop(store, scoop, found, confirm, ct.t, now_ms, head)
    if scoop.event_id is not None:
        # The confirming page's owner counts toward the event's `confirmed` label.
        await trust.confirm(store, scoop.event_id, source, scoop, published)
    if rivals:
        await trust.log(store, "flag:contested", None, str(scoop.id),
                        f"conflicting scoops {','.join(rivals)}", "resolver", scoop.source_id,
                        now_ms=now_ms)
    return CONFIRMED


async def run_candidates(store: Store, scoop_id: int, now_ms: int, v=None, resolver=None,
                         head: Optional[int] = None) -> None:
    from app.services import resolve, verifier

    v = v or verifier.get_verifier()
    resolver = resolver or resolve.get_resolver()
    async with ledger.claim_lock(scoop_id):
        for cand in await store.scoop_candidates(scoop_id, (QUEUED,)):
            scoop = await store.get_claim(scoop_id)
            if scoop is None or scoop.status != "verified":
                await store.update_scoop_candidate(cand.id, status=CLOSED)
                continue
            try:
                outcome = await check_one(store, scoop, cand, v, resolver, now_ms, head)
            except Wait:
                await store.add_job(CONFIRM_JOB, scoop_id,
                                    now_ms + verifier.JUDGE_WAIT_S * 1000)
                return
            except intake.InvalidTransition:
                outcome = CLOSED
            logger.info(f"Scoop {scoop_id} candidate {cand.id}: {outcome}")
            await store.update_scoop_candidate(
                cand.id, status=CONFIRMED if outcome == CONFIRMED else REJECTED,
                reason=None if outcome == CONFIRMED else outcome)


async def confirm_gave_up(store: Store, job: Job) -> None:
    """The scoop's found pages are dropped, so it can close as any scoop does."""
    if job.claim_id is None:
        return
    for cand in await store.scoop_candidates(job.claim_id, (QUEUED,)):
        await store.update_scoop_candidate(cand.id, status=REJECTED, reason="check_failed")


async def confirm_job(store: Store, job: Job) -> None:
    if job.claim_id is not None:
        await run_candidates(store, job.claim_id, _now_ms())
