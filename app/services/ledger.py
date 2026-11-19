"""Append-only points ledger. Rows are stamped with the epoch current when written."""

import asyncio
import logging
import weakref
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from alpharidge_ai.ledger.attestation import LEDGER_SIGNER
from alpharidge_ai.mechanism import quality as qm
from alpharidge_ai.mechanism.pools import (CAPACITY, PROVISIONAL_SHARE, WINDOW_EPOCHS, carried,
                                           window_points)
from alpharidge_ai.schema.types import MISCONDUCT_PENALTY_X, reverify_rate
from alpharidge_ai.verify.check import UNREAD
from app.config import EVENT_LANE
from app.services import intake, quality
from app.services.store import ClaimRow, LedgerRow, SourceFetchRow, Store
from app.utils import attestation_crypto as ac
from app.utils import block, mechanism_profile
from app.utils.clock import now_ms as _now_ms

logger = logging.getLogger(__name__)


PROVISIONAL, SETTLE, CLAWBACK, PENALTY = "provisional", "settle", "clawback", "penalty"
# A missed scoop's cost (`types.scoop_debt`), bound to its signed claim.
DEBT = "debt"
CARRY = "carry"
LEAF_V2 = ac.LEAF_V2
SECONDS_PER_BLOCK = 12


@dataclass(frozen=True)
class Params:
    """Ledger values from the signed mechanism profile in force, else config."""
    capacity: float
    window: int
    reverify_rate: float = 1.0
    settle_delay_h: float = 72.0


def defaults() -> Params:
    return Params(capacity=CAPACITY, window=WINDOW_EPOCHS,
                  reverify_rate=reverify_rate(), settle_delay_h=EVENT_LANE["SETTLE_DELAY_H"])


def params_from(profile: Optional[dict]) -> Params:
    base = defaults()
    if not profile:
        return base
    c = (profile.get("settlement") or {}).get("C")
    section = profile.get("ledger") or {}
    return Params(
        capacity=float(c) if c else base.capacity,
        window=int(section.get("window_epochs") or base.window),
        reverify_rate=float(section.get("reverify_rate") or base.reverify_rate),
        settle_delay_h=float(section.get("settle_delay_h") or base.settle_delay_h))


_params: Optional[Params] = None


def params() -> Params:
    """The last signed values loaded, else config."""
    return _params or defaults()


def set_params(p: Params) -> None:
    global _params
    _params = p


async def refresh_params() -> Params:
    """Capacity and window from the profile in force, else config."""
    try:
        p = params_from((await mechanism_profile.load_profiles()).get("current"))
    except Exception as e:
        logger.warning(f"No mechanism profile for the ledger: {e!r}")
        return params()
    set_params(p)
    return p


def epoch_of(head: int) -> int:
    return head // EVENT_LANE["BLOCK_LENGTH"]


async def write(store: Store, row: LedgerRow, head: Optional[int] = None) -> LedgerRow:
    """Append a row in the head's epoch, or the first after it not yet sealed. The row is
    written under the ledger lock (shared), so an epoch is never sealed between choosing it
    and the row's commit. Inside a transaction, it commits with it."""
    head = await block.current_block() if head is None else head
    if head is None:
        raise RuntimeError("chain head unavailable")
    if row.claim_id is not None and row.received_ms is None:
        claim = await store.get_claim(row.claim_id)
        if claim is not None:
            row.received_ms, row.bucket = claim.received_ms, claim.bucket
            row.coldkey = row.coldkey or claim.coldkey
    async with store.transaction() as tx:
        await tx.lock_ledger()
        row.epoch = epoch_of(head)
        # A sealed epoch never gains rows.
        while await tx.sealed_ms(LEDGER_SIGNER, row.epoch) is not None:
            row.epoch += 1
        return await tx.append_ledger(row)


def leaf(row: LedgerRow) -> dict:
    out = {
        "resource_type": "ledger", "resource_id": str(row.id), "miner_hotkey": row.hotkey,
        "validator_verdict": row.kind, "categorical_key": f"{row.category}|{row.route}",
        "points_awarded": row.points,
        "claim_id": str(row.claim_id) if row.claim_id is not None else None,
    }
    if row.received_ms is not None:
        out.update(leaf_version=LEAF_V2, received_ms=int(row.received_ms),
                   bucket=int(row.bucket or 0))
        if row.coldkey:
            out["coldkey"] = row.coldkey
        if (row.factors or {}).get("verdict"):
            out["verdict_hash"] = row.factors["verdict"]
    return out


def judged_fetch(fetches: Sequence[SourceFetchRow]) -> Optional[SourceFetchRow]:
    """The fetch whose page the page judge passed, else the last one."""
    return next((f for f in reversed(fetches) if judge_record([f])), None) or (
        fetches[-1] if fetches else None)


def judge_record(fetches: Sequence[SourceFetchRow]) -> Optional[dict]:
    """The last stored page-judge record that passed, or an unread scoop's confirm record."""
    for f in reversed(fetches):
        record = (f.check or {}).get("judge")
        if record and (record.get("outcome") == "pass" or _unread(record)):
            return record
    return None


def _unread(d: dict) -> bool:
    return bool(d.get(UNREAD))


def unread_fetch(f: Optional[SourceFetchRow]) -> bool:
    """A fetch that could not read the page, which a scoop may still cite."""
    check = (f.check or {}) if f is not None else {}
    return f is not None and (_unread(check) or _unread(check.get("judge") or {}))


def published_ms(fetches: Sequence[SourceFetchRow]) -> Optional[int]:
    """The judged page's publish time for the age factor; none for an unread page."""
    f = judged_fetch(fetches)
    return None if f is None or unread_fetch(f) else f.published_ms


async def leaves(store: Store, epoch: int) -> List[dict]:
    return [leaf(r) for r in await store.ledger_rows([epoch])]


# Rows read per query when a window is read whole.
WINDOW_PAGE = 5_000


async def window_rows(store: Store, epochs: Sequence[int]) -> List[LedgerRow]:
    """The epochs' rows by id, read a page at a time so that no single query is long."""
    out: List[LedgerRow] = []
    while True:
        page = await store.ledger_rows(epochs, after_id=out[-1].id if out else 0,
                                       limit=WINDOW_PAGE)
        out.extend(page)
        if len(page) < WINDOW_PAGE:
            return out


def _leaves(rows: Sequence[LedgerRow]) -> List[dict]:
    return [leaf(r) for r in rows]


async def window_leaves(store: Store, epoch: int, size: Optional[int] = None) -> List[dict]:
    # A large window takes a while to turn into leaves; the event loop keeps serving.
    return await asyncio.to_thread(_leaves, await window_rows(store, window(epoch, size)))


def window(epoch: int, size: Optional[int] = None) -> range:
    size = params().window if size is None else int(size)
    return range(epoch - size + 1, epoch + 1)


def window_ms() -> int:
    return params().window * EVENT_LANE["BLOCK_LENGTH"] * SECONDS_PER_BLOCK * 1000


_signing = asyncio.Lock()
# Earlier epochs a points request signs before its own.
SIGN_CATCHUP_MAX = 48


async def carry_locks(store: Store, epoch: int) -> Optional[Dict[str, float]]:
    """The locks the carry into the epoch is valued with: those of the epoch before it when
    that one was signed from the quality start, else None (raw points). The epoch's own
    locks are known only once it is signed, after its carry; validators read the same ones
    from /ledger/quality for the epoch before."""
    if qm.start_ms() is None or epoch < 1:
        return None
    until = await store.attestation_issued_ms(LEDGER_SIGNER, epoch - 1)
    if not qm.active(until):
        return None
    # `until` is the epoch's seal: every decision before it committed before the seal.
    return qm.locked(await quality.outcomes(store, qm.from_ms(), until))


async def carry_rows(store: Store, epoch: int, size: int) -> List[LedgerRow]:
    """The epoch's carry rows: debt the window would otherwise drop, valued as the weights
    value it. Read from earlier epochs only, all sealed before this one is, so the rows are
    the same whenever they are worked out."""
    gone = await leaves(store, epoch - size)
    locks = await carry_locks(store, epoch) if gone else None
    worth = None if locks is None else (lambda leaf: qm.worth(leaf, locks))
    rows = await window_rows(store, window(epoch - 1, size))
    return await asyncio.to_thread(_carry_rows, rows, gone, worth, epoch)


def _carry_rows(rows: List[LedgerRow], gone: List[dict], worth, epoch: int) -> List[LedgerRow]:
    # Epoch by epoch, as validators list the window.
    rows = sorted(rows, key=lambda r: (r.epoch, r.id))
    return [LedgerRow(
        hotkey=c["miner_hotkey"], claim_id=None, event_id=None, kind=CARRY,
        points=c["points_awarded"], epoch=epoch, route=CARRY, category=c["category"],
        received_ms=0, bucket=0, coldkey=c["coldkey"])
        for c in carried([leaf(r) for r in rows], gone, worth)]


async def seal(store: Store, epoch: int, size: int) -> int:
    """Close the epoch to new rows and fix its quality cut; returns the cut. The carry rows
    are worked out first, with no lock held, then written with the seal in one short
    transaction under the exclusive lock: every row and decision committed before it is in
    the epoch and its cut, and every later one is after both. The cut is above the store's
    clock when the lock is held and above the last seal, so it is above every stamp already
    committed (intake.decided_at), and every later stamp is at least the cut.

    Carry rows already in the epoch were left by code from before the seal, stopped between
    writing them and signing; the epoch is unsigned, so they are replaced."""
    if epoch >= 1 and await store.sealed_ms(LEDGER_SIGNER, epoch - 1) is not None and (
            await store.get_attestation(LEDGER_SIGNER, epoch - 1) is None):
        # Its carry is valued with the cut of a signed epoch before it.
        raise RuntimeError(f"epoch {epoch - 1} is sealed but not signed")
    rows = await carry_rows(store, epoch, size)
    async with store.transaction() as tx:
        at = await tx.lock_ledger(exclusive=True)
        cut = await tx.sealed_ms(LEDGER_SIGNER, epoch)
        if cut is not None:
            return cut
        # The carry is read from the epochs epoch - size to epoch - 1. Epochs are sealed in
        # order, so when both ends are sealed every one between is, and none gains rows. In
        # the first epochs signed they are not all sealed yet: read again under the lock.
        for e in {epoch - size, epoch - 1}:
            if e >= 0 and await tx.sealed_ms(LEDGER_SIGNER, e) is None:
                rows = await carry_rows(tx, epoch, size)
                break
        dropped = await tx.drop_carry(LEDGER_SIGNER, epoch)
        if dropped:
            logger.warning(f"Epoch {epoch}: replaced {dropped} carry rows left unsigned")
        for row in rows:
            await tx.append_ledger(row)
        last = await tx.latest_seal_ms(LEDGER_SIGNER)
        at = intake.decision_ms() if at is None else at
        cut = max(at, last if last is not None else at) + 1
        await tx.seal(LEDGER_SIGNER, epoch, cut)
    return cut


def _attest(leaves: List[dict], signer: str, epoch: int, keypair) -> dict:
    per_miner = window_points(leaves)
    total = round(sum(per_miner.values()), 6)
    root = ac.merkle_root(leaves)
    msg = ac.attestation_message(signer, epoch, per_miner, total, root)
    return {"perMinerPoints": per_miner, "totalPoints": total, "merkleRoot": root,
            "signature": ac.sign_attestation(keypair or ac.load_signing_key(), msg),
            "leafCount": len(leaves)}


async def _sign(store: Store, epoch: int, p: Params, keypair) -> dict:
    """Seal the epoch, then read its window and sign it with no lock or transaction held:
    once sealed its rows and quality cut no longer change, so writers and deciders go on
    while a large window is read. A signing that fails after the seal is done again from
    the same rows; the seal's cut is the attestation's issue time."""
    signer = LEDGER_SIGNER
    done = await store.get_attestation(signer, epoch)
    if done is not None:
        return done
    if await store.sealed_ms(signer, epoch) is None:
        await seal(store, epoch, p.window)
    leaves = await window_leaves(store, epoch, p.window)
    # Hashing a large window takes seconds; the event loop keeps serving meanwhile.
    cached = await asyncio.to_thread(_attest, leaves, signer, epoch, keypair)
    cached["capacity"] = float(p.capacity)
    await store.put_attestation(signer, epoch, cached)
    return await store.get_attestation(signer, epoch) or cached


class EpochTooOld(Exception):
    """An epoch never signed and too far back to sign now."""


async def signed_points(store: Store, epoch: int, p: Optional[Params] = None,
                        keypair=None, oldest: Optional[int] = None) -> dict:
    """Points per hotkey over the epoch's window, with the Merkle root, signed once and
    then served unchanged. Epochs past the last signed one are signed in order, each
    after its carry rows, at most SIGN_CATCHUP_MAX per call. An unsigned epoch before
    `oldest` or before the last sealed one is not signed."""
    p = p or params()
    cached = await store.get_attestation(LEDGER_SIGNER, epoch)
    if cached is None:
        if epoch < 0 or (oldest is not None and epoch < oldest):
            raise EpochTooOld(epoch)
        async with _signing:
            cached = await store.get_attestation(LEDGER_SIGNER, epoch)
            if cached is None:
                last = await store.last_attestation_epoch(LEDGER_SIGNER)
                sealed = await store.last_sealed_epoch(LEDGER_SIGNER)
                top = max((e for e in (last, sealed) if e is not None), default=None)
                # Epochs are sealed in order, so one before the last seal is never signed.
                if top is not None and epoch < top:
                    raise EpochTooOld(epoch)
                if last is None or last < epoch:
                    # From the epoch after the last signed one, or one sealed and not signed.
                    first = last + 1 if last is not None else (
                        sealed if sealed is not None else epoch)
                    behind = range(first, epoch)
                    for e in behind[:SIGN_CATCHUP_MAX]:
                        if await store.get_attestation(LEDGER_SIGNER, e) is None:
                            await _sign(store, e, p, keypair)
                    if len(behind) > SIGN_CATCHUP_MAX:
                        raise RuntimeError("signing_behind")
                cached = await _sign(store, epoch, p, keypair)
    lo = window(epoch, p.window)
    return {
        "epoch": epoch, "window": [lo.start, lo.stop - 1],
        "attestation": {"validatorHotkey": LEDGER_SIGNER, "epoch": epoch,
                        "perMinerPoints": cached["perMinerPoints"],
                        "totalPoints": cached["totalPoints"], "merkleRoot": cached["merkleRoot"]},
        "signature": cached["signature"], "leaf_count": cached["leafCount"],
        "capacity": cached["capacity"],
    }


async def in_window(store: Store, claim_id: int, head: int) -> float:
    """What the claim's rows still add to the current window."""
    current = window(epoch_of(head))
    return sum(r.points for r in await store.claim_ledger(claim_id) if r.epoch in current)


_claim_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def claim_lock(claim_id: int) -> asyncio.Lock:
    """Held while a claim's status and its ledger rows change together."""
    lock = _claim_locks.get(claim_id)
    if lock is None:
        lock = _claim_locks[claim_id] = asyncio.Lock()
    return lock


async def clawback(store: Store, claims: Sequence[ClaimRow], reason: str,
                   head: Optional[int] = None) -> int:
    """Take back what a claim's rows still add to the window, and cancel any pending
    settle."""
    done = 0
    for row in claims:
        async with claim_lock(row.id):
            done += await _clawback_one(store, row.id, reason, head)
    return done


async def _clawback_one(store: Store, claim_id: int, reason: str, head: Optional[int]) -> int:
    row = await store.get_claim(claim_id)
    if row is None:
        return 0
    try:
        if row.status == "verified":
            await intake.transition(store, row, "rejected", reason=reason)
        elif row.status in ("provisional", "settled"):
            head = await block.current_block() if head is None else head
            if head is None:
                raise RuntimeError("chain head unavailable")
            # The row and the move commit together: a claim left provisional or settled is
            # clawed again whole by whatever claws it next.
            async with store.transaction() as tx:
                await tx.lock_ledger()
                paid = await in_window(tx, row.id, head)
                if paid > 0:
                    await write(tx, LedgerRow(
                        hotkey=row.hotkey, claim_id=row.id, event_id=row.event_id,
                        kind=CLAWBACK, points=-paid, epoch=0, route=row.route,
                        category=row.category, reason=reason,
                        factors={"trust": row.trust, "trust_band": row.trust_band}), head)
                await intake.transition(tx, row, "clawed_back", reason=reason)
        else:
            return 0
        return 1
    except intake.InvalidTransition:
        return 0


async def penalty(store: Store, row: ClaimRow, reason: str,
                  head: Optional[int] = None) -> float:
    """A negative row of MISCONDUCT_PENALTY_X times the claim's full value, or for an unpaid
    scoop times its debt; 0 when it has neither or was already penalised. Read and written
    under the ledger lock in one transaction."""
    rows = await store.claim_ledger(row.id)
    if any(r.kind == PENALTY for r in rows) or not any(r.kind in (PROVISIONAL, DEBT)
                                                       for r in rows):
        return 0.0
    head = await block.current_block() if head is None else head
    if head is None:
        raise RuntimeError("chain head unavailable")
    async with store.transaction() as tx:
        await tx.lock_ledger()
        # Another process may be penalising it too; the second sees the first's row.
        await tx.lock_claim(row.id)
        return await _penalty(tx, row, reason, head)


async def _penalty(store: Store, row: ClaimRow, reason: str, head: int) -> float:
    rows = await store.claim_ledger(row.id)
    paid = [r for r in rows if r.kind == PROVISIONAL]
    if any(r.kind == PENALTY for r in rows):
        return 0.0
    if paid:
        pts = -MISCONDUCT_PENALTY_X * sum(r.points for r in paid) / PROVISIONAL_SHARE
    else:
        paid = [r for r in rows if r.kind == DEBT]
        if not paid:
            return 0.0
        pts = round(MISCONDUCT_PENALTY_X * paid[0].points, 6)
    if pts < 0:
        await write(store, LedgerRow(
            hotkey=row.hotkey, claim_id=row.id, event_id=row.event_id, kind=PENALTY,
            points=pts, epoch=0, route=paid[0].route, category=paid[0].category,
            reason=reason), head)
    return pts
