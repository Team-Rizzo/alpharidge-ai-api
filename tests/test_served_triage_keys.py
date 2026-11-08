"""The relevance-judging keys are served, with shadow on by default."""
import importlib


def test_judging_keys_are_served_with_shadow_on(monkeypatch):
    for k in ("SUBNET_TRIAGE_JUDGE_SHADOW", "SUBNET_TRIAGE_RELEVANCE_AUDIT_N"):
        monkeypatch.delenv(k, raising=False)
    import main
    importlib.reload(main)
    cfg = main.SUBNET_CONFIG
    for key in ("TRIAGE_JUDGE_SHADOW", "TRIAGE_JUDGE_ALL", "TRIAGE_JUDGE_MODEL", "TRIAGE_JUDGE_CONFIRM",
                "TRIAGE_REPEAT_THRESHOLD", "TRIAGE_REPEAT_PRIOR_N", "TRIAGE_REPEAT_PRIOR_MULT",
                "TRIAGE_FP_HARD", "TRIAGE_AUDIT_BORDERLINE", "TRIAGE_AUDIT_SKIP_GAZETTEER",
                "PREMIUM_SELF_CONSISTENCY", "PREMIUM_CONTENT_GATE", "BORDERLINE_VALUE_GROUNDED"):
        assert key in cfg
    assert cfg["TRIAGE_JUDGE_SHADOW"] is True
    assert cfg["TRIAGE_RELEVANCE_AUDIT_N"] == 1


def test_enforcement_is_one_served_flip(monkeypatch):
    monkeypatch.setenv("SUBNET_TRIAGE_JUDGE_SHADOW", "false")
    import main
    importlib.reload(main)
    assert main.SUBNET_CONFIG["TRIAGE_JUDGE_SHADOW"] is False


def test_reasoning_and_recycle_keys_are_served(monkeypatch):
    for k in ("SUBNET_VALIDATOR_REASONING", "SUBNET_GRADER_REASONING", "SUBNET_TRIAGE_RECYCLE_CAP"):
        monkeypatch.delenv(k, raising=False)
    import main
    importlib.reload(main)
    cfg = main.SUBNET_CONFIG
    assert cfg["VALIDATOR_REASONING"] == "off" and cfg["GRADER_REASONING"] == "off"
    assert cfg["TRIAGE_RECYCLE_CAP"] == 3
    monkeypatch.setenv("SUBNET_VALIDATOR_REASONING", "default")
    importlib.reload(main)
    assert main.SUBNET_CONFIG["VALIDATOR_REASONING"] == "default"
