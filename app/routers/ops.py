"""Operator routes: alarms, the judge audit, the trust panel, held-claim decisions,
retractions, wrong-claim metrics, conflicting claim pairs and the hotkey blacklist."""

import logging
import math
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from alpharidge_ai.verify.fetch import Paced
from app.config import EVENT_LANE
from app.routers.auth import get_operator_hotkey
from app.routers.claims import get_store
from app.routers.product import get_entities
from app.services import ops, points
from app.services.entities import EntityIndex
from app.services.store import WON, ClaimRow, Store
from app.utils import block
from app.utils.clock import now_ms as _now_ms

# Default lookback, in days, of the wrong-claim metrics and the hedge pairs.
WRONG_METRICS_DAYS = 30
HEDGE_DAYS = 7

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ops", dependencies=[Depends(get_operator_hotkey)])


class ResolveBody(BaseModel):
    action: Literal["claw", "clear"]
    reason: str = Field(min_length=1, max_length=500)


@router.get("/alarms")
async def alarms(store: Store = Depends(get_store)):
    return await ops.alarms(store, _now_ms(), await block.current_block())


@router.get("/audit")
async def judge_audit(store: Store = Depends(get_store)):
    return await ops.judge_audit(store, _now_ms())


@router.get("/trust")
async def trust_panel(store: Store = Depends(get_store)):
    return await ops.trust_panel(store, _now_ms())


@router.post("/claims/{claim_id}/resolve")
async def resolve_claim(claim_id: int, body: ResolveBody, store: Store = Depends(get_store),
                        operator: str = Depends(get_operator_hotkey)):
    row = await store.get_claim(claim_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_claim")
    if not row.held:
        raise HTTPException(status_code=409, detail="not_held")
    head = await block.current_block()
    if body.action == "claw" and head is None:
        raise HTTPException(status_code=503, detail="chain_unavailable")
    row = await points.resolve_held(store, row, body.action, head)
    logger.info(f"Operator {operator} resolved claim {claim_id}: {body.action} ({body.reason})")
    return {"claim_id": str(row.id), "status": row.status, "held": row.held}


class RetractBody(BaseModel):
    kind: Literal["retraction", "correction"] = "retraction"
    slot: Optional[str] = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=500)


async def _retract(store: Store, row: Optional[ClaimRow], body: RetractBody, operator: str,
                   entities: EntityIndex) -> dict:
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_claim")
    if row.status not in (*WON, "clawed_back"):
        raise HTTPException(status_code=409, detail="not_used")
    head = await block.current_block()
    if head is None:
        raise HTTPException(status_code=503, detail="chain_unavailable")
    done = await ops.retract(store, row, body.kind, body.slot, body.reason, operator, _now_ms(),
                             head, entities)
    logger.info(f"Operator {operator} {body.kind} on claim {row.id}: {done} ({body.reason})")
    row = await store.get_claim(row.id)
    return {"claim_id": str(row.id), "applied": done, "status": row.status,
            "wrong": row.wrong}


@router.post("/claims/{claim_id}/retract")
async def retract_claim(claim_id: int, body: RetractBody, store: Store = Depends(get_store),
                        operator: str = Depends(get_operator_hotkey),
                        entities: EntityIndex = Depends(get_entities)):
    return await _retract(store, await store.get_claim(claim_id), body, operator, entities)


@router.post("/events/{event_id}/retract")
async def retract_event(event_id: int, body: RetractBody, store: Store = Depends(get_store),
                        operator: str = Depends(get_operator_hotkey),
                        entities: EntityIndex = Depends(get_entities)):
    return await _retract(store, await ops.lead_claim(store, event_id), body, operator, entities)


class RefileBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/claims/{claim_id}/refile")
async def refile_claim(claim_id: int, body: RefileBody, store: Store = Depends(get_store),
                       operator: str = Depends(get_operator_hotkey)):
    """A claim on a filing its subject made, held or turned away before its filer could be
    confirmed: placed as a trusted claim."""
    from app.services import resolve

    row = await store.get_claim(claim_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_claim")
    head = await block.current_block()
    if head is None:
        raise HTTPException(status_code=503, detail="chain_unavailable")
    try:
        outcome = await resolve.get_resolver().refile(store, row, _now_ms(), head)
    except Paced as e:
        # The filing's index page waits its turn on the filing host; nothing was changed.
        wait = max(1, math.ceil(e.wait_s))
        raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": str(wait)})
    logger.info(f"Operator {operator} refiled claim {claim_id}: {outcome} ({body.reason})")
    row = await store.get_claim(claim_id)
    return {"claim_id": str(row.id), "outcome": outcome, "status": row.status,
            "route": row.route}


class PenaliseBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/claims/{claim_id}/penalise")
async def penalise_claim(claim_id: int, body: PenaliseBody, store: Store = Depends(get_store),
                         operator: str = Depends(get_operator_hotkey)):
    row = await store.get_claim(claim_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_claim")
    head = await block.current_block()
    if head is None:
        raise HTTPException(status_code=503, detail="chain_unavailable")
    done = await ops.penalise(store, row, body.reason, operator, _now_ms(), head)
    if done is None:
        raise HTTPException(status_code=409, detail="not_paid_or_penalised")
    logger.info(f"Operator {operator} penalised claim {claim_id}: {done} ({body.reason})")
    row = await store.get_claim(claim_id)
    return {"claim_id": str(row.id), "status": row.status, **done}


@router.get("/wrong")
async def wrong_metrics(days: Optional[float] = None, people: bool = False,
                        store: Store = Depends(get_store),
                        entities: EntityIndex = Depends(get_entities)):
    days = WRONG_METRICS_DAYS if days is None else max(0.0, days)
    return await ops.wrong_metrics(store, _now_ms() - int(days * 86_400_000), entities, people)


async def hedge_pairs(days: Optional[float] = None, store: Store = Depends(get_store),
                      entities: EntityIndex = Depends(get_entities)):
    days = HEDGE_DAYS if days is None else max(0.0, days)
    return await ops.hedge_pairs(store, _now_ms() - int(days * 86_400_000), entities)


if EVENT_LANE["SCOOPS"]:
    router.get("/hedges")(hedge_pairs)


class BlacklistBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.put("/blacklist/{hotkey}")
async def blacklist_add(hotkey: str, body: BlacklistBody, store: Store = Depends(get_store),
                        operator: str = Depends(get_operator_hotkey)):
    await store.set_blacklisted(hotkey, True, body.reason)
    logger.info(f"Operator {operator} blacklisted {hotkey} ({body.reason})")
    return {"hotkey": hotkey, "blacklisted": True}


@router.delete("/blacklist/{hotkey}")
async def blacklist_remove(hotkey: str, store: Store = Depends(get_store),
                           operator: str = Depends(get_operator_hotkey)):
    await store.set_blacklisted(hotkey, False)
    logger.info(f"Operator {operator} removed {hotkey} from the blacklist")
    return {"hotkey": hotkey, "blacklisted": False}
