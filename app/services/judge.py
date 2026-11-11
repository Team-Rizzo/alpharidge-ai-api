"""The page-judge queue. A claim that passes intake, the fixed check and novelty, or a page a
validator found for a held scoop, waits here for the listed validators: each fetches the page,
runs the fixed check and the judge, and posts its signed answers. The API combines them into
one verdict (`alpharidge_ai.judge.combine`) and stores it with every post, so the signed
ledger binds what each validator said."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import List, Optional, Tuple

from alpharidge_ai.judge.combine import combined
from alpharidge_ai.judge.prompt import JUDGE_NO, JUDGE_PROMPT_VERSION, JUDGE_YES
from alpharidge_ai.schema import kind_key, parse_kind
from app import config
from app.config import EVENT_LANE
from app.services.store import ClaimRow, JudgeItemRow, JudgePostRow, Store
from app.utils.clock import DAY_MS, iso

logger = logging.getLogger(__name__)

PASS, REJECT, UNCLEAR = "pass", "reject", "unclear"
CLAIM, CONFIRM = "claim", "confirm"
PENDING, DONE, CLOSED = "pending", "done", "closed"
# Once every listed validator has posted, or this long after the item was queued with at
# least one post, the verdict is combined.
JUDGE_QUORUM_WAIT_S = 600
# An item no listed validator posted on this long after it was queued is rejected
# `judge_unavailable`; `judge_failing` alarms well before.
JUDGE_NO_POST_REJECT_S = 24 * 3600
JUDGE_UNAVAILABLE = "judge_unavailable"
# Items served per pull, from the oldest.
PENDING_MAX = 200
POST_MAX_BYTES = 100_000


def cache_key(claim_id: int, claim, content_hash: str, filing: Optional[dict] = None) -> str:
    """The claim as judged, figures and quote included, on the API's fetch of the page; and the
    page's filing row when there is one."""
    body = json.dumps([JUDGE_PROMPT_VERSION, int(claim_id),
                       claim.model_dump(mode="json", exclude={"headline"}), content_hash,
                       *([filing] if filing else [])], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def task_of(row: ClaimRow, kind: str, url: str, at_ms: int, **extra) -> dict:
    """What validators get for an item: the miner-signed body as the re-verify sample shows
    it, the page (`url`, and `final_url` where the API's fetch of it ended), the time the
    fixed check runs at, and how the API built the claim it judges from the body (`retyped`
    first, a confirm claim on `url` with `quote` and the names it writes another way,
    `names`, then `judged_as`)."""
    return {"kind": kind, "claim_id": str(row.id), "body": row.body, "hotkey": row.hotkey,
            "signature": row.signature, "nonce": row.nonce, "signed_at": row.signed_at,
            "url": url, "at": iso(at_ms), **{k: v for k, v in extra.items() if v is not None}}


def allowance(has_passes: bool) -> int:
    """A hotkey's judge items per UTC day; hotkeys with no passes get the smaller count."""
    return EVENT_LANE["JUDGE_HOTKEY_ITEMS" if has_passes else "JUDGE_NEW_HOTKEY_ITEMS"]


def _posts(rows: List[JudgePostRow]) -> List[dict]:
    return [{"validator": p.validator, "body": p.body, "signed_at": p.signed_at,
             "nonce": p.nonce, "signature": p.signature} for p in rows]


async def combine(store: Store, item: JudgeItemRow, claim, content_hash: str,
                  filing: Optional[dict], now_ms: int) -> Optional[dict]:
    """The item's record once its verdict is due, else None."""
    listed = config.REVERIFY_VALIDATORS
    posts = sorted((p for p in await store.judge_posts([item.id])
                    if not listed or p.validator in listed), key=lambda p: p.validator)
    if not posts:
        return None
    waited = now_ms >= item.created_ms + JUDGE_QUORUM_WAIT_S * 1000
    if not waited and not listed <= {p.validator for p in posts}:
        return None
    bodies = [json.loads(p.body) for p in posts]
    return {"content_hash": content_hash, **combined(bodies, claim, filing),
            "posts": _posts(posts)}


async def page_verdict(store: Store, claim, page, claim_id: int, hotkey: str,
                       has_passes: bool, now_ms: int, task: dict,
                       filing: Optional[dict] = None, by_hotkey: bool = True,
                       panel=None) -> Optional[dict]:
    """The combined verdict for a claim on this page, from the store when it was combined
    before. None means wait and ask again; with no post at all after JUDGE_NO_POST_REJECT_S
    the claim is rejected `judge_unavailable`. A new item counts to the hotkey's allowance
    unless `by_hotkey` is off; past it the answer is unclear. `panel` stands in for the
    validators in tests."""
    key = cache_key(claim_id, claim, page.content_hash, filing)
    cached = await store.judge_verdict(key)
    if cached is not None:
        return cached
    item = await store.judge_item(key)
    if item is None:
        day = now_ms - now_ms % DAY_MS
        if by_hotkey and await store.judge_items_count(hotkey, day) >= allowance(has_passes):
            return {"outcome": UNCLEAR, "reason": "judge_allowance"}
        item = await store.add_judge_item(JudgeItemRow(
            key=key, claim_id=claim_id, kind=task["kind"], task=task, created_ms=now_ms,
            hotkey=hotkey if by_hotkey else None))
        if panel is not None:
            await panel(store, item, claim, page)
    record = await combine(store, item, claim, page.content_hash, filing, now_ms)
    if record is None:
        if now_ms < item.created_ms + JUDGE_NO_POST_REJECT_S * 1000 or await store.judge_posts(
                [item.id]):
            return None
        await store.update_judge_item(item.id, CLOSED)
        return {"outcome": REJECT, "reason": JUDGE_UNAVAILABLE}
    await store.add_judge_verdict(key, claim_id, record)
    await store.update_judge_item(item.id, DONE)
    return record


async def _live(store: Store, item: JudgeItemRow) -> bool:
    row = await store.get_claim(item.claim_id)
    want = "novelty" if item.kind == CLAIM else "verified"
    return row is not None and row.status == want


async def close_moved(store: Store, limit: int = 10_000) -> int:
    """Close pending items whose claim no longer waits for a verdict, posted on or not."""
    closed = 0
    for i in await store.judge_items(PENDING, limit=limit):
        if not await _live(store, i):
            await store.update_judge_item(i.id, CLOSED)
            closed += 1
    return closed


async def pending_for(store: Store, validator: str, limit: int = PENDING_MAX) -> List[dict]:
    """Items this validator has not posted on, oldest first; items whose claim has moved on
    are closed."""
    items = await store.judge_items(PENDING, limit=5 * limit)
    mine = {p.item_id for p in await store.judge_posts([i.id for i in items])
            if p.validator == validator}
    out = []
    for i in items:
        if i.id in mine:
            continue
        if not await _live(store, i):
            await store.update_judge_item(i.id, CLOSED)
            continue
        out.append({"key": i.key, **i.task})
        if len(out) >= limit:
            break
    return out


class PostError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _body(raw: bytes) -> dict:
    if len(raw) > POST_MAX_BYTES:
        raise PostError(413, "too_large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise PostError(400, "invalid_body")
    fixed = body.get("fixed_check") if isinstance(body, dict) else None
    if (not isinstance(body, dict) or not isinstance(body.get("key"), str)
            or not isinstance(body.get("answers"), dict) or not isinstance(fixed, dict)
            or not isinstance(fixed.get("passed"), bool)
            or not isinstance(body.get("content_hash"), str)):
        raise PostError(400, "invalid_body")
    return body


async def add_post(store: Store, validator: str, raw: bytes, signed_at: int, nonce: str,
                   signature: str, now_ms: int) -> str:
    """A validator's verdict on a pending item: accepted or duplicate."""
    body = _body(raw)
    item = await store.judge_item(body["key"])
    if item is None:
        raise PostError(404, "no_item")
    if str(body.get("claim_id")) != str(item.claim_id) or body.get("kind") != item.kind:
        raise PostError(400, "item_mismatch")
    if item.status != PENDING:
        raise PostError(409, "closed")
    got = await store.add_judge_post(JudgePostRow(
        item_id=item.id, validator=validator, body=raw.decode("utf-8"), signed_at=int(signed_at),
        nonce=nonce, signature=signature, posted_ms=now_ms))
    if got and config.REVERIFY_VALIDATORS and config.REVERIFY_VALIDATORS <= {
            p.validator for p in await store.judge_posts([item.id])}:
        # Every listed validator has posted: the claim is looked at on the next pass.
        from app.services import verifier
        verifier.get_verifier().wake(item.claim_id)
    return "accepted" if got else "duplicate"


async def failing(store: Store, now_ms: int) -> List[dict]:
    """Items no listed validator has posted on within twice the quorum wait."""
    late = now_ms - 2 * JUDGE_QUORUM_WAIT_S * 1000
    old = [i for i in await store.judge_items(PENDING, limit=10_000) if i.created_ms < late]
    posted = {p.item_id for p in await store.judge_posts([i.id for i in old])}
    waiting = [i for i in old if i.id not in posted]
    if not waiting:
        return []
    return [{"kind": "judge_failing", "waiting": len(waiting),
             "oldest_at": iso(min(i.created_ms for i in waiting))}]


_WANT = {"event": "yes", "status": "fact", "timing": "on_date", "inject": "content"}
_WANT_N = {"a": "whole", "w": "subject", "q": "same", "d": "same"}


def _wanted(name: str, a: dict):
    if a.get("type") == "noul":
        return a.get("noul")
    probs = a.get("probabilities") or {}
    if name == "office":
        want = next((o for o in ("gets", "leaves") if o in probs), None)
    elif name in _WANT:
        want = _WANT[name]
    elif name[:1] in _WANT_N and name[1:].isdigit():
        want = _WANT_N[name[0]]
    else:
        return False
    return probs.get(want)


def post_outcome(body: str) -> Tuple[Optional[str], Optional[str]]:
    """One validator's own outcome and the reason miners get for it, read from its posted
    body alone: its fixed check, then its answers banded as the combined verdict bands them
    (without the filer's-own-8-K rule). (None, None) when the body cannot be read."""
    try:
        b = json.loads(body)
        fixed, answers = b["fixed_check"], b["answers"]
    except (KeyError, TypeError, ValueError):
        return None, None
    if not isinstance(fixed, dict) or not isinstance(answers, dict):
        return None, None
    if fixed.get("passed") is not True:
        return REJECT, "fixed_check"
    bands = set()
    for name, a in answers.items():
        p = _wanted(name, a) if isinstance(a, dict) else None
        if p is False:
            continue
        ok = isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 1
        bands.add("unclear" if not ok else "yes" if p >= JUDGE_YES
                  else "no" if p < JUDGE_NO else "unclear")
    outcome = REJECT if "no" in bands else PASS if bands == {"yes"} else UNCLEAR
    return outcome, None if outcome == PASS else f"judge_{outcome}"


def retype_to(record: dict, claim) -> Optional[str]:
    """The type (and stage) to judge the claim as again: the judge did not pass it, and picked
    another open type or stage for what the page reports."""
    ta = record.get("type_answer") or {}
    if record.get("outcome") == PASS or record.get("reason") == "fixed_check" \
            or not isinstance(ta.get("p"), (int, float)) or ta["p"] < JUDGE_YES:
        return None
    kind = parse_kind(ta.get("kind") or "")
    if kind is None or kind[0].value not in EVENT_LANE["TYPES"]:
        return None
    key = kind_key(*kind)
    return None if key == kind_key(claim.event_type, claim.stage) else key
