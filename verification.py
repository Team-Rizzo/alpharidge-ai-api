"""Pure helpers for verifiable validator points (no DB/FastAPI dependencies)."""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Set


def grant_count(limit: int, outstanding: int, max_outstanding: int) -> int:
    """How many new leases to grant given an outstanding-lease cap.

    A non-positive max_outstanding means 'no cap' (unlimited) — the prod-safe default
    so the lease throttle is opt-in, not inherited from a deploy.
    """
    if int(max_outstanding) <= 0:
        return max(0, int(limit))
    return max(0, min(int(limit), int(max_outstanding) - int(outstanding)))


def clamp_points(points: float, max_points: float) -> float:
    """Clamp a per-item award into [0, max_points]."""
    p = float(points)
    if p < 0:
        return 0.0
    return min(p, float(max_points))


def compute_budget(verdict_rows: List[dict]) -> Dict[str, float]:
    """Sum points per miner over valid verdicts only."""
    out: Dict[str, float] = {}
    for r in verdict_rows:
        if r.get("validator_verdict") != "valid":
            continue
        m = r["miner_hotkey"]
        out[m] = out.get(m, 0.0) + float(r.get("points_awarded") or 0.0)
    return out


def audit_divergent_validators(group_rows: List[dict]) -> Set[str]:
    """Given verdicts sharing an audit group, return validators whose categorical_key
    is in the minority. Requires a strict majority to define 'consensus'; otherwise
    nothing is flagged (avoids penalizing on a tie)."""
    if len(group_rows) < 2:
        return set()
    counts = Counter(r["categorical_key"] for r in group_rows)
    top_key, top_n = counts.most_common(1)[0]
    if top_n * 2 <= len(group_rows):  # no strict majority
        return set()
    return {r["validator_hotkey"] for r in group_rows if r["categorical_key"] != top_key}


def has_report_consensus(reporter_count: int, threshold: int) -> bool:
    return int(reporter_count) >= int(threshold)
