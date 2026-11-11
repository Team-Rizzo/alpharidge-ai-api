"""Per-IP token buckets, in process."""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request

from app.config import EVENT_LANE

# Per-IP token buckets: rate per second and burst.
RATES = {"public": (5, 30), "claims": (2, 20), "signed": (20, 600)}
RATE_MAX_CLIENTS = 10000


class TokenBuckets:
    def __init__(self, rate: float, burst: float, max_keys: int = 10000,
                 clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.max_keys, self.clock = rate, burst, max_keys, clock
        self._buckets: "OrderedDict[str, Tuple[float, float]]" = OrderedDict()

    def take(self, key: str) -> float:
        """0 when a token was taken, else the seconds until one is free."""
        now = self.clock()
        tokens, last = self._buckets.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        wait = 0.0
        if tokens >= 1:
            tokens -= 1
        else:
            wait = (1 - tokens) / self.rate if self.rate > 0 else 60.0
        self._buckets[key] = (tokens, now)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return wait


_limits: Dict[str, TokenBuckets] = {}


def buckets(name: str) -> TokenBuckets:
    hit = _limits.get(name)
    if hit is None:
        hit = _limits[name] = TokenBuckets(*RATES[name], RATE_MAX_CLIENTS)
    return hit


def client_key(ip: Optional[str]) -> str:
    """The bucket key for a client address; IPv6 clients share one per /64."""
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return ip or "-"
    if addr.version == 4:
        return str(addr)
    if addr.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr.ipv4_mapped)


def limit(name: str):
    """A route dependency that answers 429 once the caller's IP is out of tokens."""
    async def check(request: Request) -> None:
        if not EVENT_LANE["RATE_LIMIT_ENABLED"]:
            return
        ip: Optional[str] = request.client.host if request.client else None
        wait = buckets(name).take(client_key(ip))
        if wait > 0:
            retry = max(1, round(wait))
            raise HTTPException(status_code=429, detail="rate_limited",
                                headers={"Retry-After": str(retry)})
    return check
