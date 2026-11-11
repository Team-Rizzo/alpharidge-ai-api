"""Official release times from the answer-key feeds and their calendars."""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from alpharidge_ai.schema import Claim
from app.services.answer_key.base import AK_MATCH_H, to_probe
from app.services.answer_key.match import related
from app.services.entities import EntityIndex
from app.services.store import AnswerKeyRow
from app.utils.clock import HOUR_MS

# Scheduled or answer-key types: below the prize gate, first credit and a scoop need a
# receipt after a known official release time.
NO_SCOOP_TYPES = ("monetary_policy", "economic_data", "earnings", "guidance", "election_result",
                  "match_result", "trading_halt")
RELEASE_MATCH_D = 1
RELEASE_BOOK_MAX = 20000


def _value(v) -> str:
    return getattr(v, "value", v)


class ReleaseBook:
    """Recent and upcoming release times by record, in memory."""

    def __init__(self):
        self._rows: Dict[Tuple[str, str], AnswerKeyRow] = {}

    def add(self, rows: Iterable[AnswerKeyRow], now_ms: int) -> None:
        cutoff = now_ms - int(AK_MATCH_H * HOUR_MS)
        for r in rows:
            if r.published_ms is None or r.event_type is None or r.event_date is None:
                continue
            self._rows.pop((r.adapter, r.external_id), None)
            self._rows[(r.adapter, r.external_id)] = r
        for key in [k for k, r in self._rows.items() if r.published_ms < cutoff]:
            del self._rows[key]
        while len(self._rows) > RELEASE_BOOK_MAX:
            del self._rows[next(iter(self._rows))]

    def times(self, claim: Claim, entities: EntityIndex) -> List[int]:
        """Release times of the records that could be the claim's event."""
        event_type = _value(claim.event_type)
        mine = set().union(*(entities.actor_keys(a, claim.event_type) for a in claim.actors))
        out = []
        for r in self._rows.values():
            if (r.event_type != event_type
                    or abs((r.event_date - claim.event_date).days) > RELEASE_MATCH_D
                    or not related(r.payload, claim.facts)):
                continue
            try:
                probe = to_probe(r, entities)
            except ValueError:
                continue
            if any(mine & entities.actor_keys(a, claim.event_type) for a in probe.actors):
                out.append(r.published_ms)
        return out

    def released(self, claim: Claim, received_ms: int, entities: EntityIndex) -> bool:
        """True when a release time is known and the receipt is after every matching one."""
        times = self.times(claim, entities)
        return bool(times) and received_ms >= max(times)

    def before(self, claim: Claim, received_ms: int, entities: EntityIndex) -> bool:
        """True when a release time is known and the receipt is before one."""
        times = self.times(claim, entities)
        return bool(times) and received_ms < max(times)


_book = ReleaseBook()


def get_book() -> ReleaseBook:
    return _book
