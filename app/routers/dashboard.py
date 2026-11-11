"""Public read-only dashboard routes."""

import re
import time
from collections import Counter
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from alpharidge_ai.schema import EventType
from app import config
from app.config import EVENT_LANE
from app.routers.auth import authenticate
from app.routers.claims import _can_read, get_store
from app.routers.product import get_entities, parse_time
from app.services import budget, judge, ledger, line, ops, product, scoops, trust
from app.services.entities import EntityIndex
from app.services.store import INTAKE_REJECTS, ClaimRow, MinerStats, SourceFetchRow, Store
from app.utils import block
from app.utils.auth import extract_auth_from_headers
from app.utils.clock import iso, now_ms as _now_ms

DASHBOARD_PAGE_MAX = 100

router = APIRouter(prefix="/dashboard")


_WINDOWS_MS = {"24h": 24 * 3600 * 1000, "7d": 7 * 24 * 3600 * 1000}
_KINDS = {ledger.PROVISIONAL: "provisional", ledger.SETTLE: "settled",
          ledger.CLAWBACK: "clawed", ledger.PENALTY: "penalty", ledger.DEBT: "debt"}
_TAKEN = (ledger.CLAWBACK, ledger.PENALTY, ledger.DEBT)
_TYPES = {t.value for t in EventType}
_REASON = re.compile(r"[a-z0-9_:]{1,64}")


def _points(kinds: dict) -> dict:
    out = {name: 0.0 for name in _KINDS.values()}
    for kind, pts in kinds.items():
        if kind in _KINDS:
            out[_KINDS[kind]] += -pts if kind in _TAKEN else pts
    return out


async def _carried(store: Store, category: Optional[str]) -> Dict[str, float]:
    """Debt carried forward into the last signed window, per hotkey, as a positive amount."""
    last = await store.last_attestation_epoch(ledger.LEDGER_SIGNER)
    if last is None:
        return {}
    out: Dict[str, float] = {}
    for r in await store.ledger_rows(ledger.window(last)):
        if r.kind == ledger.CARRY and category in (None, r.category):
            out[r.hotkey] = out.get(r.hotkey, 0.0) - r.points
    return out


@router.get("/miners")
async def dashboard_miners(window: str = "24h", category: Optional[str] = None,
                           limit: int = 256, store: Store = Depends(get_store)):
    """Per-hotkey claims, outcomes, budget, points by route, debt carried into the signed window
    and accuracy, from the ledger."""
    if window not in _WINDOWS_MS:
        raise HTTPException(status_code=400, detail="bad_window")
    now = _now_ms()
    until = now - product.hold_ms()
    stats = await store.miner_stats(until - _WINDOWS_MS[window], until, category)
    carried = await _carried(store, category)
    for hk in carried:
        stats.setdefault(hk, MinerStats())
    hotkeys = sorted(stats)
    week = await store.hotkey_stats(now - _WINDOWS_MS["7d"], hotkeys)
    recent = await store.miner_stats(until - budget.window_ms(), until)
    scoops = (await budget.scoop_allowances(store, hotkeys, now)
              if EVENT_LANE["SCOOPS"] else None)
    q: dict = {}
    for row in await store.miner_accuracy():
        q.setdefault(row.hotkey, {})[row.category] = round(row.q, 4)
    miners = []
    for hk in hotkeys:
        s = stats[hk]
        used = recent.get(hk)
        used = used.claims - sum(used.rejected.get(r, 0) for r in INTAKE_REJECTS) if used else 0
        points = {route: _points(kinds) for route, kinds in sorted(s.points.items())}
        miners.append({
            "hotkey": hk, "claims": s.claims, "verified": s.verified,
            "rejected": dict(sorted(s.rejected.items())),
            "budget": {"used": used, "limit": budget.budget_from(week.get(hk)),
                       **({"scoops": scoops[hk]} if scoops is not None else {})},
            "points": points,
            "carried": round(carried.get(hk, 0.0), 6),
            "total_points": round(sum(p["provisional"] + p["settled"] - p["clawed"] - p["debt"]
                                      - p["penalty"] for p in points.values())
                                  - carried.get(hk, 0.0), 6),
            "q": q.get(hk, {}),
        })
    miners.sort(key=lambda m: (-m["total_points"], m["hotkey"]))
    return {"window": window, "category": category,
            "miners": miners[:max(0, limit)], "total": len(miners)}


@router.get("/mechanism")
async def dashboard_mechanism(store: Store = Depends(get_store)):
    """The capacity, open categories, window and epoch length in force, and the total points
    of the last signed epoch."""
    p = await ledger.refresh_params()
    blocks = EVENT_LANE["BLOCK_LENGTH"]
    last = await store.last_attestation_epoch(ledger.LEDGER_SIGNER)
    att = await store.get_attestation(ledger.LEDGER_SIGNER, last) if last is not None else None
    return {"capacity": p.capacity, "categories": list(config.CATEGORIES),
            "window_epochs": p.window, "epoch_blocks": blocks,
            "epoch_seconds": blocks * ledger.SECONDS_PER_BLOCK,
            "window_hours": round(p.window * blocks * ledger.SECONDS_PER_BLOCK / 3600, 3),
            "last_signed": {"epoch": last, "total_points": att["totalPoints"]} if att else None}


SOURCES_CACHE_S = 300
_registry: Optional[trust.TrustRegistry] = None
_sources: Optional[tuple] = None


@router.get("/sources")
async def dashboard_sources(store: Store = Depends(get_store)):
    """The trusted list: each source whose pages can win an event, with its trust per
    category, and the page rules (excluded or listing pages) that apply on any source."""
    global _registry, _sources
    now = time.monotonic()
    if _sources is None or now - _sources[0] > SOURCES_CACHE_S:
        _registry = _registry or trust.TrustRegistry()
        _sources = (now, await trust.trusted_list(store, _registry, config.CATEGORIES))
    return _sources[1]


def _check(f: Optional[SourceFetchRow]) -> Optional[dict]:
    if f is None:
        return None
    extra = f.check or {}
    return {"verdict": f.verdict, "reason": f.reason, "date_gate": extra.get("date_gate"),
            "judge": (extra.get("judge") or {}).get("outcome"),
            "contrastive": extra.get("contrastive"), "unread": ledger.unread_fetch(f)}


async def _viewer(request: Request) -> Optional[str]:
    """The hotkey of a signed request, else None."""
    if extract_auth_from_headers(request) is None:
        return None
    auth = await authenticate(request, _can_read, "miner")
    return auth.ss58_address if auth is not None else None


def _open(c: ClaimRow, viewer: Optional[str], now_ms: int) -> bool:
    """A claim's source is shown to its own hotkey and operators, and to everyone once it
    settles."""
    return (c.hotkey == viewer or viewer in config.OPERATOR_HOTKEYS
            or now_ms >= (c.settle_ms or c.received_ms + product.settle_ms()))


# What each validator posted to `/ledger/reverify`; "changed" is an agree on a page that has
# since changed.
_RECHECK = {"agree": "agree", "changed": "agree", "disagree": "disagree",
            "unreachable": "unreachable"}
# The candidate shown when a validator found several pages for one scoop.
_CANDIDATE_RANK = {scoops.CONFIRMED: 3, scoops.QUEUED: 2, scoops.REJECTED: 1}


async def _validators(store: Store, page, ids, rechecks) -> Dict[int, list]:
    """Per claim, each validator that judged it, re-checked it or found a page for it."""
    by: Dict[int, Dict[str, dict]] = {}

    def entry(cid: int, hotkey: str) -> dict:
        return by.setdefault(cid, {}).setdefault(hotkey, {
            "hotkey": hotkey, "verdict": None, "reason": None, "recheck": None,
            "confirm": None})

    item_claim = {i.id: i.claim_id for i in await store.judge_items_for_claims(ids)}
    latest: Dict[tuple, object] = {}
    for p in await store.judge_posts(list(item_claim)):
        key = (item_claim[p.item_id], p.validator)
        if key not in latest or (p.posted_ms, p.id) > (latest[key].posted_ms, latest[key].id):
            latest[key] = p
    for (cid, hotkey), p in latest.items():
        e = entry(cid, hotkey)
        e["verdict"], e["reason"] = judge.post_outcome(p.body)
    for r in rechecks:
        entry(r.claim_id, r.validator)["recheck"] = _RECHECK.get(r.verdict)
    held = [c.id for c in page if c.route == "scoop"]
    best: Dict[tuple, object] = {}
    for cand in await store.candidates_for_scoops(held):
        if cand.validator == scoops.API_FINDER:
            continue
        key = (cand.scoop_id, cand.validator)
        rank = (_CANDIDATE_RANK.get(cand.status, 0), cand.id)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, cand)
    for (cid, hotkey), (_, cand) in best.items():
        entry(cid, hotkey)["confirm"] = {"status": cand.status, "reason": cand.reason}
    return {cid: [v for _, v in sorted(rows.items())] for cid, rows in by.items()}


@router.get("/items")
async def dashboard_items(request: Request, hotkey: Optional[str] = None, route: Optional[str] = None,
                          status: Optional[str] = None, category: Optional[str] = None,
                          since: Optional[str] = None, cursor: Optional[str] = None,
                          limit: int = 50, type: Optional[str] = None,
                          reason: Optional[str] = None, store: Store = Depends(get_store),
                          entities: EntityIndex = Depends(get_entities)):
    """One row per claim, with the route it took, its check result and its event's label."""
    if type is not None and type not in _TYPES:
        raise HTTPException(status_code=400, detail="bad_type")
    if reason is not None and not _REASON.fullmatch(reason):
        raise HTTPException(status_code=400, detail="bad_reason")
    now = _now_ms()
    viewer = await _viewer(request)
    since_ms = parse_time(since, "since")
    before = int(cursor) if cursor and cursor.isdigit() else None
    limit = max(1, min(int(limit), DASHBOARD_PAGE_MAX))
    rows = await store.claims_page(now - product.hold_ms(), hotkey, route, status,
                                   category, since_ms, before, limit + 1, event_type=type,
                                   reason=reason)
    page, more = rows[:limit], len(rows) > limit
    ids = [c.id for c in page]
    paid: dict = {}
    for r in await store.ledger_for_claims(ids):
        kinds = paid.setdefault(r.claim_id, {})
        kinds[r.kind] = kinds.get(r.kind, 0.0) + r.points
    checks: dict = {}
    rechecks = await store.reverify_for_claims(ids)
    validators = await _validators(store, page, ids, rechecks)
    for r in rechecks:
        counts = checks.setdefault(r.claim_id, {})
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    fetched = {f.claim_id: f for f in await store.fetches_for_claims(ids)}
    ties = await store.tie_seeds({c.bucket for c in page})
    receipts = await store.receipts_for(ids)
    events: dict = {}
    stages: dict = {}
    items = []
    for c in page:
        if c.event_id is not None and c.event_id not in events:
            ev = await store.get_event(c.event_id)
            stages[c.event_id] = ev.stage if ev is not None else None
            events[c.event_id] = await product.labelled_summary(store, ev, now, entities)
        f = fetched.get(c.id)
        url = (f.final_url if f else None) or c.source_url
        shown = _open(c, viewer, now)
        items.append({
            "id": str(c.id), "hotkey": c.hotkey, "coldkey": line.owner(c), "kind": c.kind,
            "route": c.route,
            "status": c.status, "reason": c.reason, "category": c.category,
            "event_type": c.event_type, "event_date": (c.payload or {}).get("event_date"),
            "stage": (c.payload or {}).get("stage") or stages.get(c.event_id),
            "event_id": str(c.event_id) if c.event_id is not None else None,
            **events.get(c.event_id, {"headline": None, "verification": None}),
            "received_at": iso(c.received_ms), "bucket": c.bucket,
            "tie_block": ties[c.bucket][0] if c.bucket in ties else None,
            "receipt": receipts.get(c.id),
            "check": _check(f),
            "source_domain": trust.domain_of(url) if shown else None, "trust_band": c.trust_band if shown else None,
            "points": _points(paid.get(c.id, {})), "reverify": checks.get(c.id, {}),
            "validators": validators.get(c.id, []) if shown else None,
        })
    return {"items": items, "next_cursor": str(page[-1].id) if more and page else None}


def _agreement(groups: Dict[object, Dict[str, str]], hotkey: str, outcomes) -> Optional[float]:
    matched = n = 0
    for g in groups.values():
        mine = g.get(hotkey)
        others = Counter(o for v, o in g.items() if v != hotkey and o in outcomes)
        top = others.most_common(2)
        if mine not in outcomes or not top or (len(top) > 1 and top[0][1] == top[1][1]):
            continue
        n += 1
        matched += mine == top[0][0]
    return round(matched / n, 4) if n else None


VALIDATORS_CACHE_MS = 60_000
_validators_cache: Dict[tuple, tuple] = {}


@router.get("/validators")
async def dashboard_validators(window: str = "7d", store: Store = Depends(get_store)):
    if window not in _WINDOWS_MS:
        raise HTTPException(status_code=400, detail="bad_window")
    now, key = _now_ms(), (id(store), window)
    hit = _validators_cache.get(key)
    if hit is None or now >= hit[0]:
        hit = (now + VALIDATORS_CACHE_MS, await _validator_summary(store, window, now))
        _validators_cache[key] = hit
    return hit[1]


async def _validator_summary(store: Store, window: str, now: int) -> dict:
    """Per validator hotkey seen in the window: its page verdicts and re-checks on claims open
    to everyone, the last epochs it judged and re-checked, and the re-check reports it missed.
    Agreement is the share of items where its outcome equals the most common outcome of the
    other validators on the item, leaving out items no other validator answered or where they
    tie. Cached for a minute."""
    since = now - _WINDOWS_MS[window]
    head = await block.current_block()
    posts = await store.judge_posts_since(since)
    reports = await store.reverify_reports_since(since)
    rechecks = [r for r in await store.reverify_in_epochs(sorted({e for _, e in reports}))
                if (r.validator, r.epoch) in reports]
    claims = {c.id: c for c in await store.claims_by_ids(
        {cid for cid, _ in posts} | {r.claim_id for r in rechecks})}

    def shown(cid: int) -> bool:
        return cid in claims and _open(claims[cid], None, now)

    verdicts: Dict[int, Dict[str, str]] = {}
    for cid, p in posts:
        if shown(cid):
            verdicts.setdefault(p.item_id, {})[p.validator] = judge.post_outcome(p.body)[0]
    checked: Dict[int, Dict[str, str]] = {}
    for r in rechecks:
        if shown(r.claim_id):
            checked.setdefault(r.claim_id, {})[r.validator] = _RECHECK.get(r.verdict)
    blocks_ms = ledger.SECONDS_PER_BLOCK * 1000
    last_post: Dict[str, int] = {}
    for _, p in posts:
        last_post[p.validator] = max(last_post.get(p.validator, 0), p.posted_ms)
    due, done = [], {}
    if head is not None:
        first = ledger.epoch_of(max(0, head - _WINDOWS_MS[window] // blocks_ms))
        due = await ops.reverify_due(store, head, range(first, ledger.epoch_of(head)))
        done = await store.reverify_reports(due) if due else {}
    out = []
    for hk in sorted(set(last_post) | {v for v, _ in reports}):
        mine = [e for v, e in reports if v == hk]
        listed = hk in config.REVERIFY_VALIDATORS
        out.append({
            "hotkey": hk,
            "verdicts": sum(hk in g for g in verdicts.values()),
            "verdict_agreement": _agreement(verdicts, hk, (judge.PASS, judge.REJECT,
                                                           judge.UNCLEAR)),
            "rechecks": sum(hk in g for g in checked.values()),
            "recheck_agreement": _agreement(checked, hk, ("agree", "disagree")),
            "last_judged_epoch": ledger.epoch_of(head - (now - last_post[hk]) // blocks_ms)
            if head is not None and hk in last_post else None,
            "last_rechecked_epoch": max(mine, default=None),
            "missed_epochs": sum((hk, e) not in done for e in due)
            if head is not None and listed else None,
            "last_weights_epoch": None,
        })
    return {"window": window, "validators": out}
