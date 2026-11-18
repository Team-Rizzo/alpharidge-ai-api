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


async def test_subnet_config_serves_the_pay_settings_in_force(monkeypatch):
    from types import SimpleNamespace

    from app.routers import config as config_route
    monkeypatch.setattr(config_route, "_validator_versions", {})
    monkeypatch.setattr(config_route, "_save_validator_versions", lambda: None)
    monkeypatch.setenv("EL_EVENT_PAY_X", "0.25")
    monkeypatch.delenv("EL_SCOOP_PREMIUM", raising=False)
    monkeypatch.setenv("EL_SCOOP_MISS_DEBT_X", "1")
    out = await config_route.get_subnet_config(SimpleNamespace(headers={}), "hk")
    assert out["pay"] == {"EVENT_PAY_X": 0.25, "SCOOP_PREMIUM": 5.0, "SCOOP_MISS_DEBT_X": 1.0,
                          "SCOOP_DEBT_TRUST": 1.0}


def test_the_api_never_loads_the_subnet_env_files():
    # alpharidge_ai.config loads the subnet checkout's .miner_env and .vali_env; the API keeps
    # its own .env, so no module of it may import that one. The tests' opt-out is left off
    # here.
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = ("import sys, pkgutil, importlib, app, alpharidge_ai.packages\n"
            "for m in pkgutil.walk_packages(app.__path__, 'app.'):\n"
            "    if m.name != 'app.__main__':\n"
            "        importlib.import_module(m.name)\n"
            "print('alpharidge_ai.config' in sys.modules)")
    env = {k: v for k, v in os.environ.items() if k != "ALPHARIDGE_SKIP_ENV_FILES"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, env=env,
        capture_output=True, text=True, check=True)
    assert out.stdout.split()[-1] == "False" and "[CONFIG] Loaded" not in out.stdout


def test_the_tests_skip_the_subnet_env_files():
    # conftest sets the opt-out before anything imports alpharidge_ai.config.
    import os
    import subprocess
    import sys

    assert os.environ["ALPHARIDGE_SKIP_ENV_FILES"] == "1"
    out = subprocess.run([sys.executable, "-c", "import alpharidge_ai.config"],
                         capture_output=True, text=True, check=True)
    # One line says the files are skipped; none is loaded.
    assert "[CONFIG] Loaded" not in out.stdout and "Warning" not in out.stdout
    assert "ALPHARIDGE_SKIP_ENV_FILES is set: not loading" in out.stdout
