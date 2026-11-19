"""The pay table switch: priced at each claim's receipt, served to validators as a table name."""

from datetime import datetime, timezone

import pytest

from alpharidge_ai.schema import Claim
from alpharidge_ai.schema import types as rules
from alpharidge_ai.validator.reverify import paid_right
from app import config
from app.services import ledger, points

from .pipeline import T0, Lane

START = T0 + 30 * 60_000


@pytest.fixture
def switched(monkeypatch):
    for k in ("EL_EVENT_PAY_X", "EL_SCOOP_PREMIUM", "EL_SCOOP_MISS_DEBT_X"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rules, "_switch", None)
    monkeypatch.setenv("EL_PAY_TABLE", "new")
    monkeypatch.setenv("EL_PAY_TABLE_FROM_MS", str(START))


async def _paid(received_ms):
    lane = Lane(check=True)
    await lane.sources()
    row = await lane.submit("m1", received_ms, source_url="https://newsone.com/x")
    await lane.step(received_ms + 1_000)
    row = lane.store.claims[row.id]
    paid = [r for r in await lane.store.claim_ledger(row.id) if r.kind == ledger.PROVISIONAL]
    return row, paid[0]


async def test_claims_either_side_of_the_start_pay_their_own_table_and_validators_agree(switched):
    for received_ms, base in ((START - 60_000, 12), (START + 60_000, 3)):
        row, paid = await _paid(received_ms)
        assert paid.factors["base"] == base
        item = {"route": "event", "points": paid.points, "factors": paid.factors}
        received = datetime.fromtimestamp(row.received_ms / 1000, tz=timezone.utc)
        assert paid_right(item, Claim.model_validate(row.payload), received)


def test_the_switch_is_served_as_a_table_name_and_start(switched):
    assert config.served_pay_switch() == {"pay_table": "new", "from_ms": START}
    assert points.base_points("scoop", "merger_acquisition", received_ms=START) == 120


def test_no_switch_is_served_until_one_is_set(monkeypatch):
    monkeypatch.delenv("EL_PAY_TABLE", raising=False)
    monkeypatch.setattr(rules, "_switch", None)
    assert config.served_pay_switch() is None


def test_a_bad_switch_setting_is_refused(switched, monkeypatch):
    monkeypatch.setenv("EL_PAY_TABLE", "double")
    with pytest.raises(rules.PaySettingError):
        config.served_pay_switch()
