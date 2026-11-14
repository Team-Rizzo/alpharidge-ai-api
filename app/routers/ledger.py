"""Validator reads of the event ledger: signed points, leaves, the re-verify candidates and
their results."""

import logging
import math
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from alpharidge_ai.ledger.attestation import CLOSE_GRACE_BLOCKS
from app import config
from app.routers.auth import get_validator_hotkey
from app.routers.claims import get_store
from app.services import ledger, product, quality, trust
from app.services.store import LedgerRow, ReverifyRow, Store
from app.utils import block
from app.utils.clock import iso as _iso, now_ms as _now_ms

LEDGER_PAGE_MAX = 5000
# Unsigned epochs this many windows back are not signed on request.
SIGN_BACK_WINDOWS = 2
# Re-verify results per validator per epoch: at most CAP_X × the shared re-verify rate of
# the candidates, and never fewer than CAP_MIN.
REVERIFY_CAP_X = 2.0
REVERIFY_CAP_MIN = 10

logger = logging.getLogger(__name__)
router = APIRouter()


class ReverifyItem(BaseModel):
    claim_id: str
    verdict: Literal["agree", "disagree", "unreachable"]
    observed: dict = Field(default_factory=dict)


class ReverifyBody(BaseModel):
    epoch: int
    results: List[ReverifyItem] = Field(max_length=5000)


async def _closed(epoch: int) -> int:
    """The current epoch, once `epoch` has closed."""
    head = await block.current_block()
    if head is None:
        raise HTTPException(status_code=503, detail="chain_unavailable")
    if epoch >= ledger.epoch_of(head - CLOSE_GRACE_BLOCKS):
        raise HTTPException(status_code=409, detail="epoch_open")
    return ledger.epoch_of(head)


def _limit(limit: int) -> int:
    return max(1, min(int(limit), LEDGER_PAGE_MAX))


@router.get("/ledger/points")
async def get_points(epoch: int, _: str = Depends(get_validator_hotkey),
                     store: Store = Depends(get_store)):
    if epoch < 0:
        raise HTTPException(status_code=400, detail="bad_epoch")
    now = await _closed(epoch)
    p = await ledger.refresh_params()
    try:
        return await ledger.signed_points(store, epoch, p,
                                          oldest=now - SIGN_BACK_WINDOWS * p.window)
    except ledger.EpochTooOld:
        raise HTTPException(status_code=410, detail="epoch_too_old")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/ledger/quality")
async def get_quality(from_ms: Optional[int] = None, epoch: Optional[int] = None,
                      _: str = Depends(get_validator_hotkey), store: Store = Depends(get_store)):
    """Each claim judged from from_ms that was paid or junk, as (owner index, passed), in
    judged order; with an epoch, only those judged before its points were signed, so every
    validator scores that epoch alike."""
    since = config.EVENT_LANE["QUALITY_FROM_MS"] if from_ms is None else max(0, from_ms)
    until = None
    if epoch is not None:
        until = await store.attestation_issued_ms(ledger.LEDGER_SIGNER, epoch)
        if until is None:
            raise HTTPException(status_code=409, detail="epoch_unsigned")
    owners: dict = {}
    rows = [[owners.setdefault(o, len(owners)), int(p)]
            for o, p in await quality.outcomes(store, since, until)]
    return {"from_ms": since, "until_ms": until, "owners": list(owners), "outcomes": rows}


@router.get("/ledger/leaves")
async def get_leaves(epoch: int, cursor: Optional[str] = None, limit: int = 5000,
                     _: str = Depends(get_validator_hotkey), store: Store = Depends(get_store)):
    await _closed(epoch)
    after = int(cursor) if cursor and cursor.isdigit() else 0
    rows = [r for r in await store.ledger_rows([epoch]) if r.id > after]
    page = rows[:_limit(limit)]
    more = len(rows) > len(page)
    return {"epoch": epoch, "leaves": [ledger.leaf(r) for r in page],
            "next_cursor": str(page[-1].id) if more and page else None}


async def candidates(store: Store, epoch: int) -> List[int]:
    """Claims whose provisional row is stamped with this epoch."""
    return sorted({r.claim_id for r in await store.ledger_rows([epoch])
                   if r.kind == ledger.PROVISIONAL and r.claim_id is not None})


@router.get("/ledger/sample")
async def get_sample(epoch: int, validator: str, cursor: Optional[str] = None, limit: int = 500,
                     caller: str = Depends(get_validator_hotkey),
                     store: Store = Depends(get_store)):
    if validator != caller:
        raise HTTPException(status_code=403, detail="validator_mismatch")
    await _closed(epoch)
    after = int(cursor) if cursor and cursor.isdigit() else 0
    ids = [i for i in await candidates(store, epoch) if i > after]
    page = ids[:_limit(limit)]
    rows = await store.ledger_rows([epoch])
    listed, now = caller in config.REVERIFY_VALIDATORS, _now_ms()
    items = []
    for cid in page:
        c = await store.get_claim(cid)
        if c is None:
            continue
        mine = [r for r in rows if r.claim_id == cid]
        prov = next((r for r in mine if r.kind == ledger.PROVISIONAL), None)
        paid = ledger.leaf(prov) if prov else {}
        item = {
            "claim_id": str(c.id), "hotkey": c.hotkey, "route": prov.route if prov else c.route,
            "category": prov.category if prov else c.category,
            "event_id": str(c.event_id) if c.event_id is not None else None,
            "body_sha256": c.body_hash, "signature": c.signature, "nonce": c.nonce,
            "signed_at": c.signed_at, "received_at": _iso(c.received_ms),
            "verdict_hash": paid.get("verdict_hash"),
            "points": paid.get("points_awarded"),
            "factors": {k: v for k, v in (prov.factors or {}).items()
                        if k in ("base", "T", "A")} if prov else {},
            "ledger_ids": [str(r.id) for r in mine],
        }
        # The page and body go to listed re-verifiers only: a claim's source is never shown
        # to other hotkeys.
        if not listed:
            items.append({**item, "withheld": True})
            continue
        fetches = await store.source_fetches(cid)
        f = ledger.judged_fetch(fetches)
        items.append({
            **item, "body": c.body,
            "source_url": c.source_url, "final_url": f.final_url if f else None,
            "published_at": _iso(ledger.published_ms(fetches)),
            "content_hash": f.content_hash if f else None, "verdict": f.verdict if f else None,
            "spans": f.spans if f else [],
            "judge": ledger.judge_record(fetches),
        })
    out = {"epoch": epoch, "items": items,
           "next_cursor": str(page[-1]) if len(ids) > len(page) and page else None}
    if not after:
        await store.add_reverify_pull(caller, epoch, _now_ms())
        out["settles"] = await settles(store, rows)
    return out


async def _debt(store: Store, r: LedgerRow) -> Optional[dict]:
    """The signed claim and judge record behind a debt row."""
    c = await store.get_claim(r.claim_id)
    if c is None:
        return None
    fetches = await store.source_fetches(c.id)
    return {"ledger_id": str(r.id), "debt": {
        "claim_id": str(c.id), "hotkey": c.hotkey, "body": c.body, "signature": c.signature,
        "nonce": c.nonce, "signed_at": c.signed_at, "received_at": _iso(c.received_ms),
        "published_at": _iso(ledger.published_ms(fetches)), "reason": r.reason or c.reason,
        "judge": ledger.judge_record(fetches)}}


async def settles(store: Store, rows: List[LedgerRow]) -> List[dict]:
    """The paid leaf, and its epoch, behind each settle, clawback and penalty row (for a
    penalty on an unpaid scoop, its debt leaf), and the signed claim behind each debt row and
    each such penalty."""
    out = []
    for r in rows:
        if r.kind == ledger.DEBT and r.claim_id is not None:
            d = await _debt(store, r)
            if d is not None:
                out.append(d)
            continue
        if r.kind not in (ledger.SETTLE, ledger.CLAWBACK, ledger.PENALTY) or r.claim_id is None:
            continue
        mine = await store.claim_ledger(r.claim_id)
        paid = [p for p in mine if p.kind == ledger.PROVISIONAL]
        if not paid and r.kind == ledger.PENALTY:
            paid = [p for p in mine if p.kind == ledger.DEBT]
        same = [p for p in paid if (p.route, p.category) == (r.route, r.category)]
        if same or paid:
            p = (same or paid)[0]
            entry = {"ledger_id": str(r.id), "paid_epoch": p.epoch, "paid": ledger.leaf(p)}
            if p.kind == ledger.DEBT:
                d = await _debt(store, p)
                if d is not None:
                    entry["debt"] = d["debt"]
            out.append(entry)
    return out


def result_cap(n_candidates: int) -> int:
    share = REVERIFY_CAP_X * ledger.params().reverify_rate
    return max(REVERIFY_CAP_MIN, math.ceil(share * n_candidates))


@router.post("/ledger/reverify")
async def post_reverify(body: ReverifyBody, caller: str = Depends(get_validator_hotkey),
                        store: Store = Depends(get_store)):
    await _closed(body.epoch)
    allowed = set(await candidates(store, body.epoch))
    ids = [int(r.claim_id) if r.claim_id.isdigit() else -1 for r in body.results]
    if any(i not in allowed for i in ids):
        raise HTTPException(status_code=400, detail="not_a_candidate")
    seen = await store.reverify_claims(caller, body.epoch)
    if len(seen | set(ids)) > result_cap(len(allowed)):
        raise HTTPException(status_code=400, detail="too_many_results")
    for cid, r in zip(ids, body.results):
        await record_reverify(store, cid, caller, body.epoch, r.verdict, r.observed)
    await store.add_reverify_report(caller, body.epoch, _now_ms())
    return {"accepted": len(ids)}


async def record_reverify(store: Store, claim_id: int, validator: str, epoch: int,
                          verdict: str, observed: dict) -> str:
    claim = await store.get_claim(claim_id)
    fetches = await store.source_fetches(claim_id)
    judged = ledger.judged_fetch(fetches)
    ours = judged.content_hash if judged else None
    theirs = observed.get("content_hash")
    found = ((ledger.judge_record(fetches) or {}).get("confirm") or {}).get("content_hash")
    seen = observed.get("confirm_content_hash")
    # A confirmed scoop's own page may change; its confirming page may not.
    own_changed = not found and ours and theirs and theirs != ours
    if verdict == "agree" and (own_changed or (found and seen and seen != found)):
        verdict = "changed"
        await trust.log(store, "flag:changed", ours, str(claim_id), "re-verify", validator,
                        claim.source_id if claim else None)
    holds = validator in config.REVERIFY_VALIDATORS
    if not holds:
        observed = {**observed, "advisory": True}
    await store.put_reverify(ReverifyRow(claim_id, validator, epoch, verdict, observed))
    if verdict == "disagree" and holds and claim is not None and not claim.held:
        await store.update_claim(claim.id, claim.status, held=True)
        logger.warning(f"[ALARM] Re-verify disagreement on claim {claim.id} from "
                       f"{validator[:12]}..")
    return verdict
