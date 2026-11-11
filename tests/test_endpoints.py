from app.utils import hotkey_whitelist


def test_is_miner_hotkey_uses_cached_list(monkeypatch):
    monkeypatch.setattr(hotkey_whitelist, "get_miner_hotkeys", lambda: ["minerA", "minerB"])
    assert hotkey_whitelist.is_miner_hotkey("minerA") is True
    assert hotkey_whitelist.is_miner_hotkey("not-a-miner") is False


def test_judge_settings_are_served_to_listed_validators_only(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "REVERIFY_VALIDATORS", {"listed"})
    monkeypatch.setenv("VALIDATOR_EL_JUDGE_URL", "https://judge.test/v1")
    monkeypatch.setenv("VALIDATOR_EL_JUDGE_MODEL", "m")
    monkeypatch.setenv("VALIDATOR_SCAN_INTERVAL_S", "300")
    assert config.validator_config("listed")["EL_JUDGE_MODEL"] == "m"
    other = config.validator_config("other")
    assert "EL_JUDGE_URL" not in other and other["SCAN_INTERVAL_S"] == "300"
