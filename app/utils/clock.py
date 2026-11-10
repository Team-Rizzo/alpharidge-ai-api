"""Wall-clock milliseconds."""

import time
from datetime import datetime, timezone
from typing import Optional

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def iso(ms: Optional[int]) -> Optional[str]:
    """Milliseconds as an ISO 8601 UTC time, to the millisecond."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else round(dt.timestamp() * 1000)
