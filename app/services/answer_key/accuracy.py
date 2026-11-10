"""Per-category miner accuracy Q, from answer-key checks and later outcomes. Shown on the
dashboard; it does not change pay.

    q = (correct + prior·k) / (n + k), clipped

Q is accuracy only and per category: checks in one category never move Q in another.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Sequence, Set, Tuple

from app.services import ledger, points
from app.services.store import AccuracyCheckRow, MinerAccuracyRow, Store
from app.utils import hotkey_whitelist
from app.utils.clock import DAY_MS, HOUR_MS

Q_MIN = 0.4
Q_MAX = 1.2
Q_PRIOR = 0.9
Q_K = 50
Q_WINDOW_D = 90
AK_SCAN_H = 168


ANSWER_KEY, CORROBORATION, RETRACTION, REVERIFY, CONTRADICTION = (
    "answer_key", "corroboration", "retraction", "reverify", "contradiction")
# Claws that count as wrong, by ledger reason.
CLAW_KINDS = {"clawed:retraction": RETRACTION, "clawed:reverify": REVERIFY,
              "clawed:contradicted": CONTRADICTION}


def q_value(n: int, correct: int) -> float:
    """One prior for every group, new or established."""
    q = (correct + Q_PRIOR * Q_K) / (n + Q_K)
    return min(Q_MAX, max(Q_MIN, q))


async def rating(store: Store, hotkey: str, category: str, now_ms: int) -> float:
    """Q for a hotkey in one category."""
    group = hotkey_whitelist.siblings(hotkey)
    checks = await store.accuracy_checks(now_ms - int(Q_WINDOW_D * DAY_MS),
                                         group, category)
    return q_value(len(checks), sum(c.correct for c in checks))


async def recompute(store: Store, now_ms: int) -> int:
    """Rewrite MinerAccuracy for every hotkey with checks."""
    checks = await store.accuracy_checks(now_ms - int(Q_WINDOW_D * DAY_MS))
    keys: Set[Tuple[str, str]] = {(c.hotkey, c.category) for c in checks}
    group = hotkey_whitelist.coldkey_of
    tally: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0])
    for c in checks:
        t = tally[(group(c.hotkey), c.category)]
        t[0] += 1
        t[1] += c.correct
    rows = []
    for hk, cat in sorted(keys):
        n, correct = tally.get((group(hk), cat), (0, 0))
        rows.append(MinerAccuracyRow(hk, cat, n, correct, q_value(n, correct)))
    await store.put_miner_accuracy(rows)
    return len(rows)


async def scan_outcomes(store: Store, now_ms: int, feed_types: Sequence[str]) -> int:
    """Later outcomes as checks: retraction or reverify claws, and corroboration at settle
    for types that no feed covers."""
    lo = now_ms - int(AK_SCAN_H * HOUR_MS)
    hi = now_ms + int(ledger.params().settle_delay_h * HOUR_MS)
    added = 0
    for row in await store.claims_by_settle(lo, hi, ("settled", "clawed_back")):
        if row.status == "clawed_back":
            kind = CLAW_KINDS.get(row.reason or "")
            correct = False
        elif (row.event_type not in feed_types
              and await points.coverage_owners(store, row) > 0):
            kind, correct = CORROBORATION, True
        else:
            kind = None
        if kind is not None:
            added += await store.add_accuracy_check(AccuracyCheckRow(
                claim_id=row.id, hotkey=row.hotkey, category=row.category, kind=kind,
                correct=correct, at_ms=now_ms))
    return added
