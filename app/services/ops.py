"""Operator views and actions: the trust panel, the alarm list, the judge audit, retractions,
wrong-claim metrics and conflicting claim pairs. Never part of a public response."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from alpharidge_ai.mechanism.pools import category_of
from alpharidge_ai.schema import Claim
from alpharidge_ai.verify.check import PIVOT_SLOTS
from alpharidge_ai.verify.fetch import BLOCKED, FETCH_FAILED
from app import config
from app.config import EVENT_LANE
from app.services import judge, ledger, novelty, product, trust
from app.services.answer_key import accuracy, match
from app.services.entities import EntityIndex
from app.services.store import WON, ClaimRow, Store
from app.utils import hotkey_whitelist
from app.utils.clock import DAY_MS, HOUR_MS

TRUST_PANEL_DAYS = 7
# Hours after an epoch closes by which each REVERIFY_VALIDATORS member reports on it.
REVERIFY_DEADLINE_H = 24
ALARM_LIST_MAX = 100
ALARM_LEDGER_STALE_EPOCHS = 3
ALARM_NOTICE_DAYS = 7

BAND_RANK = {"low": 0, "reported": 1, "prize-eligible": 2, "primary": 3}


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


# Trust panel

async def trust_panel(store: Store, now_ms: int) -> dict:
    """Sources promoted or demoted in the window."""
    since = now_ms - int(TRUST_PANEL_DAYS * DAY_MS)
    sources = {s.id: s for s in await store.sources()}
    promoted, demoted = [], []
    for e in await store.trust_log(since_ms=since):
        source = sources.get(e.source_id)
        if source is None:
            continue
        row = {"source": trust.label(source), "at": _iso(e.at_ms), "actor": e.actor,
               "reason": e.reason}
        if e.field.startswith("band:") and e.old in BAND_RANK and e.new in BAND_RANK:
            row.update(category=e.field[5:], old=e.old, new=e.new)
            (promoted if BAND_RANK[e.new] > BAND_RANK[e.old] else demoted).append(row)
        elif e.field == "status" and e.new in trust.BLOCKED:
            row.update(category=None, old=e.old, new=e.new)
            demoted.append(row)
    return {"since": _iso(since), "promoted": promoted, "demoted": demoted}


# Alarms

async def _reverify(store: Store) -> List[dict]:
    held = await store.held_claims(ALARM_LIST_MAX)
    by_claim: Dict[int, List[str]] = defaultdict(list)
    for r in await store.reverify_for_claims([c.id for c in held]):
        if r.verdict == "disagree":
            by_claim[r.claim_id].append(r.validator)
    return [{"kind": "reverify_disagree", "claim_id": str(c.id), "hotkey": c.hotkey,
             "route": c.route, "category": c.category, "validators": sorted(by_claim[c.id])}
            for c in held]


async def _fetches(store: Store, now_ms: int) -> List[dict]:
    p = config.PRIVATE
    window_h = p["ALARM_FETCH_WINDOW_H"]
    rows = await store.fetches_since(now_ms - int(window_h * HOUR_MS))
    failed = [f for f in rows if f.reason in (FETCH_FAILED, BLOCKED)]
    if len(rows) < max(1, p["ALARM_FETCH_MIN"]):
        return []
    share = len(failed) / len(rows)
    if share < p["ALARM_FETCH_FAIL_SHARE"]:
        return []
    domains = Counter(trust.domain_of(f.final_url or "") or "?" for f in failed)
    return [{"kind": "fetch_failures", "window_h": window_h, "fetches": len(rows),
             "failed": len(failed), "share": round(share, 4),
             "domains": [{"domain": d, "failed": n} for d, n in domains.most_common(10)]}]


async def _concentration(store: Store, head: Optional[int]) -> List[dict]:
    if head is None:
        return []
    per_hotkey: Dict[str, float] = defaultdict(float)
    for r in await store.ledger_rows(ledger.window(ledger.epoch_of(head))):
        per_hotkey[r.hotkey] += r.points
    groups: Dict[str, List[str]] = defaultdict(list)
    totals: Dict[str, float] = defaultdict(float)
    for hk, pts in per_hotkey.items():
        if pts > 0:
            ck = hotkey_whitelist.coldkey_of(hk)
            groups[ck].append(hk)
            totals[ck] += pts
    total = sum(totals.values())
    p = config.PRIVATE
    shared = len(totals) >= p["ALARM_COLDKEY_MIN"]
    many = p["ALARM_COLDKEY_HOTKEYS"]
    return [{"kind": "coldkey_concentration", "coldkey": ck, "hotkeys": sorted(groups[ck]),
             "points": round(pts, 6), "share": round(pts / total, 4)}
            for ck, pts in sorted(totals.items(), key=lambda x: -x[1])
            if total > 0 and (shared and pts / total > p["ALARM_COLDKEY_SHARE"]
                              or len(groups[ck]) >= many)]


async def _categories(store: Store, head: Optional[int]) -> List[dict]:
    """Window rows in a category that is not open."""
    if head is None:
        return []
    rows = await ledger.window_leaves(store, ledger.epoch_of(head))
    found = Counter(category_of(r) for r in rows)
    return [{"kind": "unknown_category", "category": c, "rows": n}
            for c, n in sorted(found.items()) if c not in config.CATEGORIES]


def staleness(head: Optional[int], last: Optional[int]) -> dict:
    """How far the newest signed epoch trails the newest closed one."""
    closed = ledger.epoch_of(head) - 1 if head is not None else None
    behind = closed - last if closed is not None and last is not None else None
    minutes = (behind * EVENT_LANE["BLOCK_LENGTH"] * ledger.SECONDS_PER_BLOCK / 60
               if behind is not None else None)
    return {"closed_epoch": closed, "signed_epoch": last, "epochs_behind": behind,
            "minutes_behind": minutes}


def _stale(metric: dict) -> List[dict]:
    behind = metric["epochs_behind"]
    if metric["closed_epoch"] is None or (
            behind is not None and behind < ALARM_LEDGER_STALE_EPOCHS):
        return []
    return [{"kind": "ledger_stale", **metric}]


async def reverify_due(store: Store, head: int, epochs) -> List[int]:
    """Of `epochs`, those with paid claims whose re-verify report deadline has passed."""
    deadline = int(REVERIFY_DEADLINE_H * 3600
                   / (EVENT_LANE["BLOCK_LENGTH"] * ledger.SECONDS_PER_BLOCK))
    due = [e for e in epochs if e >= 0 and ledger.epoch_of(head) - 1 - e >= deadline]
    if not due:
        return []
    return sorted({r.epoch for r in await store.ledger_rows(due)
                   if r.kind == ledger.PROVISIONAL and r.claim_id is not None})


async def _missed(store: Store, head: Optional[int]) -> List[dict]:
    """Epochs a re-verifying validator has not reported on by the deadline."""
    if head is None or not config.REVERIFY_VALIDATORS:
        return []
    paid = await reverify_due(store, head, ledger.window(ledger.epoch_of(head) - 1))
    if not paid:
        return []
    reports = await store.reverify_reports(paid)
    out = [{"kind": "reverify_missed", "validator": v, "epoch": e,
            "deadline_h": REVERIFY_DEADLINE_H}
           for e in sorted(paid, reverse=True) for v in sorted(config.REVERIFY_VALIDATORS)
           if (v, e) not in reports]
    return out[:ALARM_LIST_MAX]


async def _gaps(store: Store, now_ms: int) -> List[dict]:
    rows = await match.gap_alerts(store, now_ms)
    return [{"kind": "answer_key_gap", **{k: v for k, v in r.items() if k != "observed_ms"},
             "observed_at": _iso(r["observed_ms"])}
            for r in rows[:ALARM_LIST_MAX]]


NOTICE_KINDS = {"flag:notice": "retraction_notice", "flag:changed": "page_changed"}


async def _notices(store: Store, now_ms: int) -> List[dict]:
    """Retraction or correction notices and changed pages seen on re-fetch or re-verify, for
    review."""
    since = now_ms - int(ALARM_NOTICE_DAYS * DAY_MS)
    out = {}
    for e in await store.trust_log(since_ms=since):
        kind = NOTICE_KINDS.get(e.field)
        if kind is None or not (e.new or "").isdigit():
            continue
        claim = await store.get_claim(int(e.new))
        if claim is None or claim.wrong is not None:
            continue
        out[(kind, e.new)] = {
            "kind": kind, "claim_id": e.new, "hotkey": claim.hotkey,
            "event_id": str(claim.event_id) if claim.event_id is not None else None,
            "source_url": claim.source_url, "trust_band": claim.trust_band,
            "status": claim.status, "by": e.actor, "reason": e.reason, "at": _iso(e.at_ms)}
    return list(out.values())[:ALARM_LIST_MAX]


async def _claws(store: Store, now_ms: int) -> List[dict]:
    """Coldkeys with many of their recently paid claims clawed back, for review."""
    p = config.PRIVATE
    since = now_ms - int(p["ALARM_CLAW_DAYS"] * DAY_MS)
    tally: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for c in await store.banded_claims(since):
        if c.status in ("provisional", "settled", "clawed_back"):
            t = tally[c.coldkey or hotkey_whitelist.coldkey_of(c.hotkey)]
            t[0] += 1
            t[1] += c.status == "clawed_back"
    return [{"kind": "coldkey_claws", "coldkey": ck, "paid": n, "clawed": k,
             "share": round(k / n, 4), "days": p["ALARM_CLAW_DAYS"]}
            for ck, (n, k) in sorted(tally.items())
            if k >= p["ALARM_CLAW_MIN"] and k / n >= p["ALARM_CLAW_SHARE"]][:ALARM_LIST_MAX]


async def _contradictions(store: Store, now_ms: int) -> List[dict]:
    """Claims a trusted page disagreed with, for review."""
    since = now_ms - int(ALARM_NOTICE_DAYS * DAY_MS)
    out = {}
    for e in await store.trust_log(actor="resolver", since_ms=since):
        if e.field != trust.FLAG_CONTRADICTED or not (e.new or "").isdigit():
            continue
        claim = await store.get_claim(int(e.new))
        if claim is None or claim.wrong is not None:
            continue
        out[e.new] = {"kind": "trusted_contradiction", "claim_id": e.new,
                      "hotkey": claim.hotkey, "source_url": claim.source_url,
                      "reason": e.reason, "at": _iso(e.at_ms)}
    return list(out.values())[:ALARM_LIST_MAX]


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


async def judge_audit(store: Store, now_ms: int) -> dict:
    """Claims the page judge passed that an official feed matched: the share the feed agrees
    with, its 95% lower bound, and the ones it does not. Of the last AUDIT_N such checks
    within AUDIT_DAYS; alarm once there are AUDIT_MIN_N and the bound is below AUDIT_ALARM_LB."""
    p = config.PRIVATE
    since = now_ms - int(p["AUDIT_DAYS"] * DAY_MS)
    checks = sorted((c for c in await store.accuracy_checks(since)
                     if c.kind == accuracy.ANSWER_KEY),
                    key=lambda c: (c.at_ms, c.id))[-p["AUDIT_N"]:]
    n, k = len(checks), sum(c.correct for c in checks)
    lower = round(wilson_lower(k, n), 4)
    return {"since": _iso(since), "checks": n, "agree": k,
            "accuracy": round(k / n, 4) if n else None, "lower_bound": lower,
            "alarm_below": p["AUDIT_ALARM_LB"], "min_checks": p["AUDIT_MIN_N"],
            "alarm": n >= p["AUDIT_MIN_N"] and lower < p["AUDIT_ALARM_LB"],
            "disagree": [{"claim_id": str(c.claim_id), "hotkey": c.hotkey, "record": c.ref,
                          "at": _iso(c.at_ms)} for c in checks if not c.correct][:ALARM_LIST_MAX]}


async def _audit(store: Store, now_ms: int) -> List[dict]:
    a = await judge_audit(store, now_ms)
    if not a["alarm"]:
        return []
    return [{"kind": "judge_accuracy", **{k: a[k] for k in ("checks", "agree", "lower_bound",
                                                           "alarm_below")}}]


async def alarms(store: Store, now_ms: int, head: Optional[int]) -> dict:
    metric = staleness(head, await store.last_attestation_epoch(ledger.LEDGER_SIGNER))
    out = [*await _reverify(store), *await judge.failing(store, now_ms),
           *await _fetches(store, now_ms), *await _concentration(store, head),
           *await _gaps(store, now_ms), *_stale(metric),
           *await _missed(store, head),
           *await _categories(store, head), *await _notices(store, now_ms),
           *await _claws(store, now_ms),
           *await _contradictions(store, now_ms), *await _audit(store, now_ms)]
    return {"at": _iso(now_ms), "chain_head": head, "ledger": metric, "alarms": out,
            "counts": dict(sorted(Counter(a["kind"] for a in out).items()))}


# Retractions and corrections

RETRACTION, CORRECTION = "retraction", "correction"


def corrects(claim: Claim, slot: Optional[str]) -> bool:
    """A correction counts only when it changes the actor, type, stage or a fact the claim
    asserted."""
    slot = (slot or "").strip().lower()
    if slot in ("actor", "type"):
        return True
    if slot == "stage":
        return claim.stage is not None
    return bool(slot) and any(f.field.lower() == slot for f in claim.facts)


async def retract(store: Store, claim: ClaimRow, kind: str, slot: Optional[str], reason: str,
                  operator: str, now_ms: int, head: Optional[int],
                  entities: EntityIndex) -> bool:
    """An operator retraction or correction of a used claim. False when it does not count."""
    if kind == CORRECTION and not corrects(Claim.model_validate(claim.payload), slot):
        return False
    mark = trust.CORRECTED if kind == CORRECTION else trust.RETRACTED
    done = await trust.proved_wrong(store, trust.RETRACTION, claim, reason, operator, now_ms,
                                    head, mark=mark)
    if done and claim.event_id is not None:
        await product.sync_events(store, [claim.event_id], now_ms, entities)
    return done


async def penalise(store: Store, claim: ClaimRow, reason: str, operator: str, now_ms: int,
                   head: int) -> Optional[dict]:
    """Operator-confirmed misconduct: the claw while the claim is still clawable, and a
    points penalty that nets across the coldkey. None when it was never paid or already
    penalised. An unpaid scoop is penalised on its debt."""
    rows = await store.claim_ledger(claim.id)
    if not any(r.kind in (ledger.PROVISIONAL, ledger.DEBT) for r in rows) or any(
            r.kind == ledger.PENALTY for r in rows):
        return None
    clawed = trust.clawable(claim, now_ms) and await ledger.clawback(
        store, [claim], "clawed:misconduct", head) > 0
    async with ledger.claim_lock(claim.id):
        pts = await ledger.penalty(store, claim, "penalty:misconduct", head)
    await trust.log(store, "penalty:misconduct", None, str(claim.id), reason, operator,
                    claim.source_id, now_ms=now_ms)
    return {"clawed": clawed, "penalty": round(pts, 6)}


async def lead_claim(store: Store, event_id: int) -> Optional[ClaimRow]:
    ev = await store.get_event(event_id)
    if ev is None:
        return None
    return await product._lead(store, ev, await store.claims_for_event(ev.id, WON))


# Wrong-claim metrics

async def wrong_metrics(store: Store, since_ms: int, entities: EntityIndex,
                        people: bool = False) -> dict:
    """Per hotkey and stored trust band: claims, claims proved wrong, and points earned,
    clawed and penalised. `people` keeps claims about people that are served unverified."""
    claims = await store.banded_claims(since_ms)
    if people:
        claims = [c for c in claims
                  if product.labelled_claim(Claim.model_validate(c.payload), entities)]
    by_id = {c.id: c for c in claims}
    rows: Dict[tuple, dict] = {}

    def row(c: ClaimRow) -> dict:
        return rows.setdefault((c.hotkey, c.trust_band), {
            "hotkey": c.hotkey, "band": c.trust_band, "claims": 0, "wrong": 0,
            "earned": 0.0, "clawed": 0.0, "penalty": 0.0, "debt": 0.0})

    for c in claims:
        r = row(c)
        r["claims"] += 1
        r["wrong"] += c.wrong is not None
    for e in await store.ledger_for_claims(list(by_id)):
        r = row(by_id[e.claim_id])
        if e.kind == ledger.CLAWBACK:
            r["clawed"] += -e.points
        elif e.kind == ledger.PENALTY:
            r["penalty"] += -e.points
        elif e.kind == ledger.DEBT:
            r["debt"] += -e.points
        else:
            r["earned"] += e.points
    out = sorted(rows.values(), key=lambda r: (r["hotkey"], BAND_RANK.get(r["band"], -1)))
    for r in out:
        for k in ("earned", "clawed", "penalty", "debt"):
            r[k] = round(r[k], 6)
    return {"since": _iso(since_ms), "people": people, "rows": out}


# Conflicting claim pairs

def _slot_value(claim: Claim, slot: str) -> List[str]:
    if slot == "stage":
        return [claim.stage.value] if claim.stage else []
    return [*(str(f.value) for f in claim.facts if f.field == slot),
            *(a.name or a.id for a in claim.actors if a.role == slot)]


def _side(c: ClaimRow, claim: Claim, slot: str) -> dict:
    return {"claim_id": str(c.id), "event_id": None if c.event_id is None else str(c.event_id),
            "hotkey": c.hotkey, "coldkey": hotkey_whitelist.coldkey_of(c.hotkey),
            "received": _iso(c.received_ms), "value": _slot_value(claim, slot),
            "route": c.route, "status": c.status, "reason": c.reason, "wrong": c.wrong}


async def hedge_pairs(store: Store, since_ms: int, entities: EntityIndex) -> dict:
    """Claims on one event or pivot that fill the decisive slot differently."""
    decisive = novelty.DECISIVE_SLOTS
    gap_s = config.PRIVATE["HEDGE_GAP_S"]
    gap_ms = int(gap_s * 1000)
    by_type: Dict[str, list] = defaultdict(list)
    for c in await store.claims_of_types(since_ms, list(decisive)):
        claim = Claim.model_validate(c.payload)
        pivot = PIVOT_SLOTS.get(c.event_type)
        keys = novelty.slot_keys(claim, pivot, entities) if pivot else set()
        by_type[c.event_type].append((c, claim, keys))
    pairs = []
    for event_type, rows in sorted(by_type.items()):
        buckets: Dict[str, List[int]] = defaultdict(list)
        for i, (c, _, keys) in enumerate(rows):
            for k in ({f"event:{c.event_id}"} if c.event_id is not None else set()) | keys:
                buckets[k].append(i)
        near = sorted({(i, j) for ids in buckets.values()
                       for n, i in enumerate(ids) for j in ids[n + 1:]})
        for i, j in near:
            (a, ca, _), (b, cb, _) = rows[i], rows[j]
            same_event = a.event_id is not None and a.event_id == b.event_id
            same_coldkey = (hotkey_whitelist.coldkey_of(a.hotkey)
                            == hotkey_whitelist.coldkey_of(b.hotkey))
            apart = abs(b.received_ms - a.received_ms)
            if not (same_coldkey or apart <= gap_ms):
                continue
            if not novelty.decided_otherwise(ca, cb, entities):
                continue
            slot = decisive[event_type]
            pairs.append({"event_type": event_type, "slot": slot,
                          "same_event": same_event, "same_coldkey": same_coldkey,
                          "apart_s": round(apart / 1000, 3),
                          "claims": [_side(a, ca, slot), _side(b, cb, slot)]})
    return {"since": _iso(since_ms), "gap_s": gap_s, "pairs": pairs}
