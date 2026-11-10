"""Line order among claims that compete for the same prize."""

import hashlib
from typing import Dict, List

from app.services import budget
from app.services.store import ClaimRow, Store
from app.utils import block
from app.utils.receipt import TIE_BUCKET_MS

TIE_GRACE_MS = 250


def closes_ms(bucket: int) -> int:
    return (bucket + 1) * TIE_BUCKET_MS


def draw(block_hash: str, coldkey: str) -> str:
    return hashlib.sha256(f"{block_hash}:{coldkey}".encode()).hexdigest()


def owner(claim: ClaimRow) -> str:
    """The coldkey stored at receipt, else the hotkey."""
    return claim.coldkey or claim.hotkey


async def tie_seeds(store: Store, buckets) -> Dict[int, str]:
    """The tie block hash of each bucket that has one: the first finalised block stamped at
    or after the bucket closed. Looked up on the chain once, then kept."""
    got = await store.tie_seeds(buckets)
    for b in set(buckets) - set(got):
        found = await block.tie_block(closes_ms(b))
        if found is not None:
            await store.put_tie_seed(b, *found)
            got[b] = found
    return {b: h for b, (_, h) in got.items()}


async def in_line_order(store: Store, claims: List[ClaimRow]) -> List[ClaimRow]:
    """By receipt bucket, then by a per-coldkey draw on the bucket's tie block, then by id."""
    seeds = {b: h for b, (_, h) in (await store.tie_seeds({c.bucket for c in claims})).items()}
    return sorted(claims, key=lambda c: (c.bucket, draw(seeds.get(c.bucket, ""), owner(c)), c.id))


def ready(claim: ClaimRow, now_ms: int) -> bool:
    """True once the claim's bucket has closed, plus the grace period."""
    return now_ms >= closes_ms(claim.bucket) + TIE_GRACE_MS


async def ready_claims(store: Store, now_ms: int, limit: int = 500) -> List[ClaimRow]:
    """Queued claims within their hotkey's budget that may be checked now, in line order. A
    claim that shares its bucket waits for the bucket's tie block."""
    caps = await budget.limits(store, await store.queued_hotkeys(), now_ms)
    rows = [c for c in await store.admissible_claims(caps, now_ms - budget.window_ms(), limit)
            if ready(c, now_ms)]
    members = await store.bucket_claim_ids({c.bucket for c in rows})
    shared = {b for b, ids in members.items() if len(ids) > 1}
    seeds = await tie_seeds(store, shared) if shared else {}
    return await in_line_order(store, [c for c in rows
                                       if c.bucket not in shared or c.bucket in seeds])
