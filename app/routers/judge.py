"""The page-judge queue for listed validators: items to judge, and their signed verdicts."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app import config
from app.config import EVENT_LANE
from app.routers.auth import authenticate, no_query
from app.routers.claims import get_store
from app.services import judge
from app.services.store import Store
from app.utils import hotkey_whitelist
from app.utils.clock import now_ms as _now_ms

router = APIRouter()


async def _listed(request: Request):
    auth = await authenticate(request, hotkey_whitelist.is_validator_hotkey, "validator")
    if auth is None:
        raise HTTPException(status_code=401, detail="Missing authentication headers.")
    if auth.ss58_address not in config.REVERIFY_VALIDATORS:
        raise HTTPException(status_code=403, detail="not_listed")
    return auth


@router.get("/judge/pending")
async def get_pending(request: Request, store: Store = Depends(get_store)):
    auth = await _listed(request)
    return {"types": list(EVENT_LANE["TYPES"]),
            "items": await judge.pending_for(store, auth.ss58_address)}


@router.post("/judge/verdicts")
async def post_verdict(request: Request, store: Store = Depends(get_store)):
    no_query(request)
    auth = await _listed(request)
    try:
        status = await judge.add_post(store, auth.ss58_address, await request.body(),
                                      int(auth.timestamp), auth.nonce, auth.signature, _now_ms())
    except judge.PostError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return JSONResponse({"status": status}, status_code=202 if status == "accepted" else 200)
