"""Per-hotkey daily check budget, earned by verified claims."""

from typing import Dict, Iterable, Optional, Tuple

from app.config import EVENT_LANE
from app.services.store import ClaimRow, HotkeyStats, Store
from app.utils import hotkey_whitelist
from app.utils.clock import DAY_MS, HOUR_MS

CHECK_BUDGET_PER_PASS = 2
CHECK_BUDGET_WINDOW_H = 24
# A coldkey with this many judged scoops in the window, under this share confirmed, holds no
# new ones.
SCOOP_ALLOW_MIN_JUDGED = 10
SCOOP_ALLOW_LOW_RATE = 0.10
SCOOP_ALLOW_WINDOW_D = 30


async def budget(store: Store, hotkey: str, now_ms: int) -> int:
    stats = (await store.hotkey_stats(now_ms - 7 * DAY_MS, [hotkey])).get(hotkey)
    return budget_from(stats)


def budget_from(stats: Optional[HotkeyStats]) -> int:
    """The daily check limit from a hotkey's last 7 days of judged claims."""
    passes = stats.passes if stats else 0
    return EVENT_LANE["CHECK_BUDGET_BASE"] + CHECK_BUDGET_PER_PASS * passes


def window_ms() -> int:
    return int(CHECK_BUDGET_WINDOW_H * HOUR_MS)


async def limits(store: Store, hotkeys: Iterable[str], now_ms: int) -> Dict[str, int]:
    hotkeys = list(hotkeys)
    stats = await store.hotkey_stats(now_ms - 7 * DAY_MS, hotkeys) if hotkeys else {}
    return {hk: budget_from(stats.get(hk)) for hk in hotkeys}


async def exhausted(store: Store, hotkey: str, limit: int, now_ms: int) -> Optional[int]:
    """Seconds until the hotkey may send again, or None while it has budget left."""
    used, oldest = await store.budget_use(hotkey, now_ms - window_ms())
    if used < limit:
        return None
    free_ms = (oldest + window_ms() - now_ms) if oldest is not None else window_ms()
    return max(1, -(-free_ms // 1000))


async def within_budget(store: Store, claim: ClaimRow, now_ms: int) -> bool:
    """True when the claim is among the hotkey's first `limit` claims of the last day."""
    limit = await budget(store, claim.hotkey, now_ms)
    return await store.claims_ahead(claim, now_ms - window_ms()) < limit


def scoop_allowance(record: Optional[Tuple[int, int]]) -> bool:
    """Whether a coldkey may hold new scoops, from its (hits, judged) scoop record."""
    hits, judged = record or (0, 0)
    return judged < SCOOP_ALLOW_MIN_JUDGED or hits / judged >= SCOOP_ALLOW_LOW_RATE


async def scoop_allowances(store: Store, hotkeys: Iterable[str],
                           now_ms: int) -> Dict[str, bool]:
    """Each hotkey's allowance, from its coldkey's scoop record."""
    owners = {hk: hotkey_whitelist.coldkey_of(hk) for hk in hotkeys}
    since = now_ms - int(SCOOP_ALLOW_WINDOW_D * DAY_MS)
    records = await store.scoop_record(since, set(owners.values()))
    return {hk: scoop_allowance(records.get(ck)) for hk, ck in owners.items()}


async def scoop_limited(store: Store, row: ClaimRow, now_ms: int) -> bool:
    """The claim's coldkey has missed too many of its recent scoops."""
    owner = row.coldkey or row.hotkey
    since = now_ms - int(SCOOP_ALLOW_WINDOW_D * DAY_MS)
    return not scoop_allowance((await store.scoop_record(since, [owner])).get(owner))
