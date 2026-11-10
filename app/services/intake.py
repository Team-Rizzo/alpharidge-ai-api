"""Claim intake and lifecycle transitions."""

import json
import secrets
from typing import Optional, Tuple

from pydantic import ValidationError

from alpharidge_ai.schema import (
    Claim, ClaimKind, canonical_body, category_of, has_required, text_problem,
)
from app.config import EVENT_LANE
from app.services import budget
from app.services.store import ClaimRow, DuplicateClaim, Store
from app.utils import hotkey_whitelist
from app.utils.auth import AuthRequest, body_sha256
from app.utils.receipt import bucket, to_ms

MAX_BODY_BYTES = 16384
# Slot values a type pays for; a claim with another value, or without one, is turned away
# unchecked.
PAID_VALUES = {"personnel": ("role", ("ceo", "cfo", "chair", "president", "coo"))}

FETCH_JOB = "fetch"
# A trusted report on an event another claim took, fetched only to count its owner.
REPORT_JOB = "report"

EDGES = {
    "received": {"queued", "rejected"},
    "queued": {"novelty", "rejected"},
    "novelty": {"fetched", "rejected"},
    "fetched": {"verified", "rejected"},
    "verified": {"provisional", "rejected"},
    "provisional": {"settled", "clawed_back"},
    "settled": {"clawed_back"},
}


class IntakeError(Exception):
    def __init__(self, status: int, detail: str, retry_after: Optional[int] = None):
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


class InvalidTransition(Exception):
    pass


async def transition(store: Store, claim: ClaimRow, to: str, **fields) -> ClaimRow:
    if to not in EDGES.get(claim.status, ()):
        raise InvalidTransition(f"{claim.status} -> {to}")
    if not await store.update_claim(claim.id, claim.status, status=to, **fields):
        raise InvalidTransition(f"claim {claim.id} is no longer {claim.status}")
    return await store.get_claim(claim.id)


def off_reason(claim: Claim) -> Optional[str]:
    """Why intake turns a claim away unchecked, or None."""
    if claim.kind is not ClaimKind.EVENT:
        return "kind_off"
    if claim.event_type.value not in EVENT_LANE["TYPES"]:
        return "type_off"
    slot, paid = PAID_VALUES.get(claim.event_type.value, (None, ()))
    if slot is None:
        return None
    values = [str(f.value).strip().lower() for f in claim.facts if f.field == slot]
    if not values or any(v not in paid for v in values):
        return "role_off"
    return None


def check_size(body: bytes) -> None:
    if len(body) > MAX_BODY_BYTES:
        raise IntakeError(413, "body_too_large")


def parse(body: bytes) -> Claim:
    try:
        claim = Claim.model_validate_json(body)
    except (ValidationError, ValueError) as e:
        raise IntakeError(400, "invalid_schema") from e
    if canonical_body(claim) != body or text_problem(claim):
        raise IntakeError(400, "invalid_schema")
    if claim.kind is ClaimKind.EVENT and not (has_required(claim) and claim.quote):
        raise IntakeError(400, "missing_required")
    return claim


async def accept(store: Store, body: bytes, auth: AuthRequest,
                 received_ns: int) -> Tuple[ClaimRow, bool]:
    """Store a signed claim. Returns (row, created); a re-sent body returns the first row."""
    check_size(body)
    hotkey = auth.ss58_address
    if await store.is_blacklisted(hotkey):
        raise IntakeError(403, "Access denied.")
    received_ms = to_ms(received_ns)
    claim = parse(body)
    body_hash = body_sha256(body)
    existing = await store.claim_by_body(hotkey, body_hash)
    if existing is not None:
        return existing, False

    category = category_of(claim.event_type).value
    off = off_reason(claim)
    on = off is None
    if on:
        limit = await budget.budget(store, hotkey, received_ms)
        wait = await budget.exhausted(store, hotkey, limit, received_ms)
        if wait is not None:
            raise IntakeError(429, "budget_exhausted", retry_after=wait)
    coldkey = hotkey_whitelist.coldkey_of(hotkey)
    row = ClaimRow(
        hotkey=hotkey, signature=auth.signature, nonce=auth.nonce or secrets.token_hex(16),
        signed_at=int(auth.timestamp), body_hash=body_hash, body=body.decode("utf-8"),
        received_ms=received_ms, bucket=bucket(received_ms), kind=claim.kind.value,
        category=category, event_type=claim.event_type.value, payload=json.loads(body),
        source_url=claim.source_url,
        status="queued" if on else "rejected", reason=off,
        coldkey=coldkey if coldkey != hotkey else None)
    try:
        return await store.create_claim(row, jobs=(FETCH_JOB,) if on else ()), True
    except DuplicateClaim:
        existing = await store.claim_by_body(hotkey, body_hash)
        if existing is None:
            raise IntakeError(401, "Invalid authentication. Signature verification failed.")
        return existing, False
