"""Open scoops for listed validators, and the pages they find for them."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config
from app.routers.auth import get_validator_hotkey
from app.routers.claims import get_store
from app.services import scoops
from app.services.entities import shared_index
from app.services.store import Store
from app.utils.clock import now_ms as _now_ms

router = APIRouter()


class ConfirmBody(BaseModel):
    scoop_id: int
    url: str = Field(min_length=8, max_length=2048, pattern=r"^https?://")


def _listed(caller: str) -> None:
    if caller not in config.REVERIFY_VALIDATORS:
        raise HTTPException(status_code=403, detail="not_listed")


@router.get("/scoops/open")
async def get_open(caller: str = Depends(get_validator_hotkey),
                   store: Store = Depends(get_store)):
    _listed(caller)
    return {"scoops": await scoops.open_scoops(store, shared_index(), _now_ms()),
            "feeds": list(scoops.feeds())}


@router.post("/scoops/confirm")
async def post_confirm(body: ConfirmBody, caller: str = Depends(get_validator_hotkey),
                       store: Store = Depends(get_store)):
    _listed(caller)
    status = await scoops.submit(store, body.scoop_id, body.url, caller, _now_ms())
    if status is None:
        raise HTTPException(status_code=404, detail="no_scoop")
    return JSONResponse({"status": status}, status_code=202 if status == "queued" else 200)
