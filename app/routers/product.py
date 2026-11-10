"""Product reads: events, entities and the feed."""

import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app import config
from app.config import EVENT_LANE
from app.routers.auth import authenticate
from app.routers.claims import _can_read, get_store
from app.services import product, resolve
from app.services.entities import EntityIndex
from app.services.store import EventQuery, Store
from app.utils.auth import extract_auth_from_headers
from app.utils.clock import now_ms as _now_ms

PRODUCT_PAGE_MAX = 100
FEED_PAGE_MAX = 500

router = APIRouter()


def get_entities() -> EntityIndex:
    return resolve.get_resolver().index()


async def product_caller(request: Request, store: Store = Depends(get_store)) -> str:
    """An API key (env list or table), or a signed miner or validator request."""
    key = request.headers.get("X-API-Key")
    if key:
        digest = hashlib.sha256(key.encode()).hexdigest()
        owner = (f"env:{digest[:16]}" if digest in config.PRODUCT_API_KEYS
                 else await store.api_key_owner(digest))
        if owner is None:
            raise HTTPException(status_code=401, detail="invalid_api_key")
        request.state.api_key = True
        return owner
    if not EVENT_LANE["PRODUCT_KEYS_REQUIRED"]:
        return "public"
    if extract_auth_from_headers(request) is not None:
        auth = await authenticate(request, _can_read, "miner")
        if auth is not None:
            return auth.ss58_address
    raise HTTPException(status_code=401, detail="api_key_required")


def parse_time(text: Optional[str], name: str) -> Optional[int]:
    if not text:
        return None
    try:
        return product.parse_ms(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad_{name}")


def _limit(limit: int, cap: int) -> int:
    return max(1, min(int(limit), cap))


def _cursor(text: Optional[str]):
    if not text:
        return None
    first, _, eid = text.partition(":")
    if not (first.lstrip("-").isdigit() and eid.isdigit()):
        raise HTTPException(status_code=400, detail="bad_cursor")
    return int(first), int(eid)


def _keys(entities: EntityIndex, entity: Optional[str]):
    if not entity:
        return ()
    try:
        return product.entity_keys(entities, entity)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_entity")


def _shown(request: Request, out):
    """Sources are shown once the claims behind them settle."""
    return product.hide_sources(out, _now_ms())


async def _page(store: Store, entities: EntityIndex, q: EventQuery) -> dict:
    limit = q.limit
    q.limit += 1
    rows = await store.list_events(q)
    page = rows[:limit]
    more = len(rows) > limit
    return {"items": [await product.summary(store, e, entities) for e in page],
            "next_cursor": f"{page[-1].first_seen_ms or 0}:{page[-1].id}"
            if more and page else None}


def _unverified(request: Request, include_unverified: bool) -> bool:
    """Unverified events are for API-key callers only, when enabled."""
    return (include_unverified and EVENT_LANE["API_UNVERIFIED"]
            and getattr(request.state, "api_key", False))


def _confidences(confidence: Optional[str], include_unverified: bool) -> tuple:
    allowed = product.PUBLIC + ((product.UNVERIFIED,) if include_unverified else ())
    return tuple(c for c in allowed if confidence in (None, c))


@router.get("/events")
async def list_events(request: Request, category: Optional[str] = None,
                      type: Optional[str] = None,
                      stage: Optional[str] = None, entity: Optional[str] = None,
                      since: Optional[str] = None, until: Optional[str] = None,
                      confidence: Optional[str] = None, include_unverified: bool = False,
                      cursor: Optional[str] = None, limit: int = 50,
                      _: str = Depends(product_caller), store: Store = Depends(get_store),
                      entities: EntityIndex = Depends(get_entities)):
    q = EventQuery(published_by_ms=_now_ms(),
                   confidences=_confidences(confidence,
                                            _unverified(request, include_unverified)),
                   category=category, event_type=type, stage=stage,
                   entity_keys=_keys(entities, entity), since_ms=parse_time(since, "since"),
                   until_ms=parse_time(until, "until"), before=_cursor(cursor),
                   limit=_limit(limit, PRODUCT_PAGE_MAX))
    if not q.confidences:
        return {"items": [], "next_cursor": None}
    return _shown(request, await _page(store, entities, q))


@router.get("/events/{event_id}")
async def event_detail(request: Request, event_id: str, include_unverified: bool = False,
                       _: str = Depends(product_caller), store: Store = Depends(get_store),
                       entities: EntityIndex = Depends(get_entities)):
    ev = await store.get_event(int(event_id)) if event_id.isdigit() else None
    if not product.visible(ev, _now_ms(), _unverified(request, include_unverified)):
        raise HTTPException(status_code=404, detail="not_found")
    return _shown(request, await product.detail(store, ev, entities))


@router.get("/entities/{entity_id}")
async def entity_timeline(request: Request, entity_id: str, cursor: Optional[str] = None, limit: int = 50,
                          _: str = Depends(product_caller), store: Store = Depends(get_store),
                          entities: EntityIndex = Depends(get_entities)):
    scheme, _sep, ext = entity_id.partition(":")
    rec = entities.get(scheme, ext) if ext else None
    if rec is None:
        raise HTTPException(status_code=404, detail="not_found")
    q = EventQuery(published_by_ms=_now_ms(), confidences=product.PUBLIC,
                   entity_keys=rec.keys, before=_cursor(cursor),
                   limit=_limit(limit, PRODUCT_PAGE_MAX))
    page = await _page(store, entities, q)
    return {"entity": product.entity_json(rec), "events": _shown(request, page["items"]),
            "next_cursor": page["next_cursor"]}


@router.get("/feed")
async def feed(request: Request, since: Optional[str] = None, category: Optional[str] = None,
               type: Optional[str] = None, entity: Optional[str] = None, limit: int = 100,
               _: str = Depends(product_caller), store: Store = Depends(get_store),
               entities: EntityIndex = Depends(get_entities)):
    after = int(since) if since and since.isdigit() else 0
    rows = await store.feed(after_seq=after, until_ms=_now_ms() - product.hold_ms(),
                            category=category, event_type=type,
                            entity_keys=_keys(entities, entity),
                            limit=_limit(limit, FEED_PAGE_MAX))
    return {"items": _shown(request, [product.record_json(r) for r in rows]),
            "next_cursor": str(rows[-1].seq) if rows else str(after)}
