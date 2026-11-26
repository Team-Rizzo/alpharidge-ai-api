"""The public mechanism view shows the pay tables in force and the quality pay start."""

import pytest

from alpharidge_ai.mechanism import quality as qm
from alpharidge_ai.schema import types as rules
from app.routers.dashboard import pay_schedule

NEW, LEAD = 1_795_080_600_000, 1_795_653_000_000


@pytest.fixture
def switched(monkeypatch):
    monkeypatch.setattr(rules, "_switch", None)
    monkeypatch.setenv("EL_PAY_TABLE", "new,lead")
    monkeypatch.setenv("EL_PAY_TABLE_FROM_MS", f"{NEW},{LEAD}")


def test_no_switch_shows_todays_table(monkeypatch):
    monkeypatch.delenv("EL_PAY_TABLE", raising=False)
    monkeypatch.setattr(rules, "_switch", None)
    out = pay_schedule()
    assert [s["table"] for s in out["steps"]] == ["today"]
    assert out["current"] == {"table": "today", "from_ms": None, "event_pay_x": 1.0,
                              "scoop_premium": 5.0, "scoop_miss_debt_x": 2.0}


def test_steps_and_current_follow_the_switch(switched):
    out = pay_schedule(now_ms=NEW + 1)
    assert [(s["table"], s["from_ms"]) for s in out["steps"]] == [
        ("today", None), ("new", NEW), ("lead", LEAD)]
    assert out["current"]["table"] == "new" and out["current"]["from_ms"] == NEW
    assert out["current"]["event_pay_x"] == 0.25
    lead = pay_schedule(now_ms=LEAD)["current"]
    assert lead["table"] == "lead" and lead["scoop_lead"]["full_minutes"] == 30


def test_quality_pay_start_is_its_own_field(monkeypatch):
    monkeypatch.setattr(qm, "_served_ms", None)
    monkeypatch.setattr(qm, "QUALITY_FROM", None)
    monkeypatch.setenv("EL_QUALITY_FROM_MS", "1795131000000")
    assert qm.start_ms() == 1795131000000
