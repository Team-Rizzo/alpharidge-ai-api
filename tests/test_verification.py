import verification as v


def test_grant_count_respects_outstanding_cap():
    assert v.grant_count(limit=10, outstanding=28, max_outstanding=30) == 2
    assert v.grant_count(limit=10, outstanding=30, max_outstanding=30) == 0
    assert v.grant_count(limit=10, outstanding=35, max_outstanding=30) == 0
    assert v.grant_count(limit=10, outstanding=0, max_outstanding=30) == 10


def test_grant_count_unlimited_when_cap_non_positive():
    # §2 prod-safe default: max_outstanding <= 0 means no cap (full limit granted).
    assert v.grant_count(limit=10, outstanding=999, max_outstanding=0) == 10
    assert v.grant_count(limit=10, outstanding=999, max_outstanding=-1) == 10
    assert v.grant_count(limit=0, outstanding=5, max_outstanding=0) == 0


def test_budget_sums_only_valid_verified_verdicts():
    rows = [
        {"miner_hotkey": "m1", "validator_verdict": "valid", "points_awarded": 1.0},
        {"miner_hotkey": "m1", "validator_verdict": "valid", "points_awarded": 1.0},
        {"miner_hotkey": "m1", "validator_verdict": "invalid", "points_awarded": 1.0},
        {"miner_hotkey": "m2", "validator_verdict": "valid", "points_awarded": 1.0},
    ]
    per_miner = v.compute_budget(rows)
    assert per_miner == {"m1": 2.0, "m2": 1.0}


def test_clamp_points_per_item():
    assert v.clamp_points(5.0, max_points=1.0) == 1.0
    assert v.clamp_points(0.5, max_points=1.0) == 0.5
    assert v.clamp_points(-3.0, max_points=1.0) == 0.0


def test_audit_divergence_flags_minority():
    group = [
        {"validator_hotkey": "A", "categorical_key": "BTC|bull"},
        {"validator_hotkey": "B", "categorical_key": "BTC|bull"},
        {"validator_hotkey": "C", "categorical_key": "ETH|bear"},
    ]
    diverged = v.audit_divergent_validators(group)
    assert diverged == {"C"}


def test_audit_divergence_no_majority_returns_empty():
    group = [
        {"validator_hotkey": "A", "categorical_key": "BTC|bull"},
        {"validator_hotkey": "B", "categorical_key": "ETH|bear"},
    ]
    assert v.audit_divergent_validators(group) == set()


def test_report_consensus_threshold():
    assert v.has_report_consensus(reporter_count=2, threshold=2) is True
    assert v.has_report_consensus(reporter_count=1, threshold=2) is False
