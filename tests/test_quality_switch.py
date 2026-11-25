"""The quality start: set by EL_QUALITY_FROM_MS on the API, served to validators as a time."""

import pytest

from alpharidge_ai.mechanism import quality as qm
from app import config
from app.routers.ledger import served_starts

START = 1_795_100_000_000


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(qm, "_served_ms", None)
    monkeypatch.setattr(qm, "QUALITY_FROM", None)
    monkeypatch.delenv("EL_QUALITY_FROM_MS", raising=False)


def test_no_start_is_served_until_one_is_set():
    assert config.served_quality_switch() is None
    assert not qm.active(START + 1)


def test_the_start_is_served_and_turns_quality_on(monkeypatch):
    monkeypatch.setenv("EL_QUALITY_FROM_MS", str(START))
    assert config.served_quality_switch() == {"from_ms": START}
    assert qm.active(START) and not qm.active(START - 1)
    assert START in served_starts()


def test_a_bad_start_setting_is_refused(monkeypatch):
    monkeypatch.setenv("EL_QUALITY_FROM_MS", "soon")
    with pytest.raises(qm.QualitySettingError):
        config.served_quality_switch()


def test_the_quality_start_does_not_restart_the_budget_score(monkeypatch):
    from app.services import quality as svc
    monkeypatch.setenv("EL_QUALITY_FROM_MS", str(START))
    monkeypatch.setitem(config.EVENT_LANE, "BUDGET_FROM_MS", 0)
    assert svc.start() != START
    assert svc.start() < START


def test_the_lead_warning_is_only_for_a_start_still_ahead():
    now = START
    lead = config.PAY_SWITCH_LEAD_MS
    assert config.switch_too_close(now + lead - 1, now)
    assert not config.switch_too_close(now + lead, now)
    assert not config.switch_too_close(now, now)
    assert not config.switch_too_close(now - 1, now)
    assert not config.switch_too_close(now - 30 * 86_400_000, now)
    assert not config.switch_too_close(None, now)
