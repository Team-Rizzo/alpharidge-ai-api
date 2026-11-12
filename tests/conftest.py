import os
import sys

import pytest

# Make the `app` package importable from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The deployment's .env never reaches the tests.
os.environ["API_SKIP_DOTENV"] = "1"
# Disabling auth keeps endpoint tests from needing a live metagraph.
os.environ.setdefault("AUTH_ENABLED", "false")
# Fixed key so runs are reproducible.
os.environ.setdefault("EL_RECHECK_KEY", "test-recheck-key")
# Synthetic values; production values differ.
for _k, _v in {"RECHECK_SAMPLE_RATE": "0.37", "RECHECK_LOW_TRUST": "0.6", "RECHECK_MIN_H": "2",
               "RECHECK_MAX_H": "5", "RECHECK_ACCEPT_LANGUAGE": "xx",
               "RECHECK_NOTICES": "zz-notice-a,zz-notice-b", "PAGE_FLAG_DAYS": "3",
               "PAGE_FLAG_BYLINES_MIN": "8", "PAGE_FLAG_BYLINES_SHARE": "0.5",
               "PAGE_FLAG_CONTRIBUTOR_MIN": "2", "PAGE_FLAG_UNDATED_MIN": "4",
               "PAGE_FLAG_UNDATED_SHARE": "0.6", "PAGE_FLAG_MARKERS": "contributor,guest post",
               "ALARM_CLAW_DAYS": "5", "ALARM_CLAW_MIN": "2", "ALARM_CLAW_SHARE": "0.4",
               "ALARM_COLDKEY_HOTKEYS": "9", "ALARM_COLDKEY_MIN": "4",
               "ALARM_COLDKEY_SHARE": "0.3", "ALARM_FETCH_WINDOW_H": "2",
               "ALARM_FETCH_MIN": "12", "ALARM_FETCH_FAIL_SHARE": "0.4", "AUDIT_DAYS": "40",
               "AUDIT_N": "150", "AUDIT_MIN_N": "40", "AUDIT_ALARM_LB": "0.9",
               "HEDGE_GAP_S": "90", "TRUST_SPIKE_X": "4", "TRUST_SPIKE_MIN": "30",
               "TRUST_SPIKE_BASE_DAYS": "20"
               }.items():
    os.environ.setdefault(f"EL_{_k}", _v)
# A synthetic receipt-signing key; production sets its own.
os.environ.setdefault("API_ATTESTATION_PRIVKEY", "7e" * 32)
# Tests never reach a registry or a real chain.
os.environ.setdefault("EL_REGISTRY", "off")
os.environ.setdefault("SUBTENSOR_NETWORK", "mock")
# Status replies are read fresh in tests unless a test turns the cache on.
os.environ.setdefault("EL_STATUS_CACHE_S", "0")


@pytest.fixture
def scoops_on(monkeypatch):
    from app.config import EVENT_LANE
    monkeypatch.setitem(EVENT_LANE, "SCOOPS", True)


@pytest.fixture(autouse=True)
def _fresh_status_replies():
    from app.routers import claims
    claims._status.clear()
    yield
    claims._status.clear()
