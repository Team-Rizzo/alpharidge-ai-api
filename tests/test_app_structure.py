from types import SimpleNamespace

import pytest

from app.utils import block, hotkey_whitelist


class _FakeMetagraph:
    hotkeys = ["v1", "v2", "m1", "m2"]
    coldkeys = ["c1", "c2", "c3", "c3"]
    validator_permit = [1, 1, 0, 0]
    S = [hotkey_whitelist.STAKE_THRESHOLD, 0, 0, 0]

    def sync(self, subtensor=None, lite=True):
        pass


class _FakeSubtensor:
    def __init__(self, head=100, fail=False):
        self.head, self.fail, self.metagraph_calls = head, fail, 0

    def get_current_block(self):
        if self.fail:
            raise ConnectionError("down")
        return self.head

    def metagraph(self, netuid):
        self.metagraph_calls += 1
        return _FakeMetagraph()


@pytest.fixture
def real_chain(monkeypatch):
    monkeypatch.setattr(block, "NETWORK", "finney")


@pytest.fixture
def fake_chain(real_chain, monkeypatch, tmp_path):
    sub = _FakeSubtensor()
    monkeypatch.setattr(block, "_subtensor", sub)
    monkeypatch.setattr(block, "_block", None)
    monkeypatch.setattr(block, "_block_at", 0.0)
    monkeypatch.setattr(hotkey_whitelist, "_MINER_HOTKEYS_FILE", tmp_path / "m.json")
    monkeypatch.setattr(hotkey_whitelist, "_VALIDATOR_HOTKEYS_FILE", tmp_path / "v.json")
    monkeypatch.setattr(hotkey_whitelist, "_miners", [])
    monkeypatch.setattr(hotkey_whitelist, "_validators", [])
    monkeypatch.setattr(hotkey_whitelist, "_coldkeys", {})
    return sub


def test_block_is_cached_and_survives_a_failed_read(fake_chain, monkeypatch):
    assert block.get_current_block() == 100
    fake_chain.head = 101
    assert block.get_current_block() == 100
    monkeypatch.setattr(block, "_block_at", 0.0)
    fake_chain.fail = True
    monkeypatch.setattr(block, "get_subtensor", lambda: fake_chain)
    assert block.get_current_block() == 100


def test_block_is_none_without_any_read(real_chain, monkeypatch):
    monkeypatch.setattr(block, "_block", None)
    monkeypatch.setattr(block, "_block_at", 0.0)
    monkeypatch.setattr(block, "get_subtensor", lambda: _FakeSubtensor(fail=True))
    assert block.get_current_block() is None


@pytest.mark.asyncio
async def test_one_sync_fills_both_lists(fake_chain, monkeypatch):
    monkeypatch.setattr(hotkey_whitelist, "ALLOW_MANUAL_HOTKEYS", False)
    assert await hotkey_whitelist.sync_async() is True
    assert fake_chain.metagraph_calls == 1
    assert sorted(hotkey_whitelist.get_miner_hotkeys()) == ["m1", "m2"]
    assert hotkey_whitelist.get_validator_hotkeys() == ["v1"]
    assert hotkey_whitelist.coldkey_of("m2") == "c3" and hotkey_whitelist.coldkey_of("x") == "x"
    assert hotkey_whitelist.is_validator_hotkey("v2") is False


@pytest.mark.asyncio
async def test_require_internal_uses_internal_allowed_ips(monkeypatch):
    from fastapi import HTTPException

    from app import config
    from app.routers.auth import require_internal

    monkeypatch.setattr(config, "INTERNAL_ALLOWED_IPS", {"127.0.0.1", "10.0.0.5"})
    await require_internal(SimpleNamespace(client=SimpleNamespace(host="10.0.0.5")))
    with pytest.raises(HTTPException):
        await require_internal(SimpleNamespace(client=SimpleNamespace(host="10.0.0.6")))


def test_app_mounts_every_router():
    from app.main import app

    paths = {r.path for r in app.routes}
    for p in ("/health", "/config/subnet", "/mechanism/profile", "/dashboard/miners", "/dashboard/items", "/events",
              "/events/{event_id}", "/entities/{entity_id}", "/feed", "/claims",
              "/claims/{claim_id}", "/ops/hedges", "/scoops/open", "/scoops/confirm"):
        assert p in paths
    assert not {"/rewards", "/reputation/snapshot", "/penalties", "/attestation", "/reports",
                "/verdicts", "/blacklist", "/webhooks", "/price/tao-usd",
                "/axon/check", "/mechanism/proposal", "/dashboard/stats", "/dashboard/preview",
                "/dashboard/articles/sources", "/dashboard/narratives"} & paths


def test_logging_drops_per_request_httpx_lines(monkeypatch):
    import logging

    from app.main import _setup_logging

    monkeypatch.setattr(logging.getLogger(), "level", logging.INFO)
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)
    _setup_logging()
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
