"""Local runs: with SUBTENSOR_NETWORK=mock the chain head follows the clock and no chain is read."""

from app.utils import block, hotkey_whitelist


def _no_chain():
    raise AssertionError("the chain was read")


def test_mock_head_follows_the_clock(monkeypatch):
    monkeypatch.setattr(block, "NETWORK", "mock")
    monkeypatch.setattr(block, "MOCK_GENESIS_TS", 1000.0)
    monkeypatch.setattr(block, "MOCK_BLOCK_SECONDS", 0.5)
    monkeypatch.setattr(block, "get_subtensor", _no_chain)
    assert block.mock_block(1000.0) == 0 and block.mock_block(1050.2) == 100
    assert block.get_current_block() >= block.mock_block(1050.2)


def test_mock_whitelist_uses_manual_lists(monkeypatch):
    monkeypatch.setattr(block, "NETWORK", "mock")
    monkeypatch.setattr(block, "get_subtensor", _no_chain)
    monkeypatch.setattr(hotkey_whitelist, "ALLOW_MANUAL_HOTKEYS", True)
    monkeypatch.setattr(hotkey_whitelist, "MANUAL_MINER_HOTKEYS", ["5Miner"])
    monkeypatch.setattr(hotkey_whitelist, "MANUAL_VALIDATOR_HOTKEYS", ["5Vali"])
    monkeypatch.setattr(hotkey_whitelist, "MOCK_COLDKEYS", {"5Miner": "5Cold"})
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {})
    assert hotkey_whitelist.sync()
    assert hotkey_whitelist.coldkey_of("5Miner") == "5Cold"
    assert hotkey_whitelist.coldkey_of("5Vali") == "5Vali"
    assert hotkey_whitelist.is_miner_hotkey("5Miner")
    assert hotkey_whitelist.is_validator_hotkey("5Vali")
    assert not hotkey_whitelist.is_miner_hotkey("5Vali")
