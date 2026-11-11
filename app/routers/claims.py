"""Miner claim intake and status."""

import time
from collections import OrderedDict
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app import config, db
from app.config import EVENT_LANE
from app.routers.auth import authenticate, no_query
from app.services import intake, product
from app.services.store import ClaimRow, PrismaStore, Store
from app.utils import hotkey_whitelist
from app.utils.clock import iso as _iso
from app.utils.receipt import received_ns

router = APIRouter()

_store: Optional[Store] = None

STATUS_CACHE_MAX = 50_000
# Claim id -> (expiry, owner hotkey, reply).
_status: "OrderedDict[int, Tuple[float, str, dict]]" = OrderedDict()


def get_store() -> Store:
    global _store
    if _store is None:
        _store = PrismaStore(db.prisma)
    return _store


def _receipt(c: ClaimRow) -> dict:
    return {"id": str(c.id), "status": c.status, "received_at": _iso(c.received_ms),
            "bucket": c.bucket}


def _can_read(hotkey: str) -> bool:
    return (hotkey_whitelist.is_miner_hotkey(hotkey) or hotkey_whitelist.is_validator_hotkey(hotkey)
            or hotkey in config.OPERATOR_HOTKEYS)


def _unauthenticated() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail="Missing authentication headers.")


_in_flight = 0


@router.post("/claims", status_code=status.HTTP_202_ACCEPTED)
async def post_claim(request: Request, store: Store = Depends(get_store)):
    global _in_flight
    no_query(request)
    if _in_flight >= EVENT_LANE["CLAIMS_IN_FLIGHT"]:
        return JSONResponse(status_code=503, headers={"Retry-After": "1"},
                            content={"detail": "busy", "retry_after": 1})
    _in_flight += 1
    try:
        return await _post_claim(request, store)
    finally:
        _in_flight -= 1


async def _post_claim(request: Request, store: Store):
    body = await request.body()
    received = received_ns(request)
    try:
        intake.check_size(body)
        auth = await authenticate(request, hotkey_whitelist.is_miner_hotkey, "miner")
        if auth is None:
            raise _unauthenticated()
        claim, created = await intake.accept(store, body, auth, received)
    except intake.IntakeError as e:
        if e.retry_after is not None:
            return JSONResponse(status_code=e.status, headers={"Retry-After": str(e.retry_after)},
                                content={"detail": e.detail, "retry_after": e.retry_after})
        raise HTTPException(status_code=e.status, detail=e.detail)
    return JSONResponse(status_code=202 if created else 200,
                        content={**_receipt(claim), "receipt": await store.receipt_of(claim.id)})


async def _event_shown(store: Store, claim: ClaimRow) -> Tuple[bool, Optional[str]]:
    """The event is named once it is published, or to the claim that opened it, with its
    verification label."""
    if claim.event_id is None:
        return True, None
    ev = await store.get_event(claim.event_id)
    if ev is None:
        return False, None
    now_ms = time.time_ns() // 1_000_000
    if ev.first_claim_id == claim.id or product.visible(ev, now_ms):
        return True, ev.confidence if ev.confidence != product.LINE else None
    return False, None


@router.get("/claims/{claim_id}")
async def get_claim(claim_id: str, request: Request, store: Store = Depends(get_store)):
    auth = await authenticate(request, _can_read, "miner")
    cid = int(claim_id) if claim_id.isdigit() else None
    now = time.monotonic()
    hit = _status.get(cid) if cid is not None else None
    if hit is not None and hit[0] > now:
        owner, reply = hit[1], hit[2]
    else:
        claim = await store.get_claim(cid) if cid is not None else None
        if claim is None:
            raise HTTPException(status_code=404, detail="not_found")
        owner, reply = claim.hotkey, await _status_reply(store, claim)
        ttl = EVENT_LANE["STATUS_CACHE_S"]
        if ttl > 0:
            _status.pop(cid, None)
            _status[cid] = (now + ttl, owner, reply)
            while len(_status) > STATUS_CACHE_MAX:
                _status.popitem(last=False)
    if (auth is not None and owner != auth.ss58_address
            and not hotkey_whitelist.is_validator_hotkey(auth.ss58_address)
            and auth.ss58_address not in config.OPERATOR_HOTKEYS):
        raise HTTPException(status_code=404, detail="not_found")
    return reply


async def _status_reply(store: Store, claim: ClaimRow) -> dict:
    points = await store.claim_points(claim.id)
    shown, verification = await _event_shown(store, claim)
    fetch = next((f for f in reversed(await store.source_fetches(claim.id)) if f.check), None)
    detail = (fetch.check.get("detail") if fetch and claim.status == "rejected"
              and fetch.reason == claim.reason else None)
    return {
        **_receipt(claim),
        "receipt": await store.receipt_of(claim.id),
        "reason": claim.reason, "reason_detail": detail,
        "novelty": claim.novelty if shown else None,
        "route": claim.route,
        "event_id": str(claim.event_id) if shown and claim.event_id is not None else None,
        "verification": verification,
        "category": claim.category,
        "points": {"provisional": points.get("provisional", 0.0),
                   "settled": points.get("settle", 0.0),
                   "clawed_back": 0.0 - points.get("clawback", 0.0),
                   "debt": 0.0 - points.get("debt", 0.0)},
        "settle_at": _iso(claim.settle_ms),
    }
