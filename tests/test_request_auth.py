import hashlib
import json
import secrets
import time

import pytest
from bittensor_wallet import Keypair
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.routers import config as config_router
from app.routers.auth import get_validator_hotkey
from app.utils import auth, hotkey_whitelist

VALI = Keypair.create_from_seed("0x" + "c3" * 32)
OTHER = Keypair.create_from_seed("0x" + "d4" * 32)


def _headers(kp, method, target, body=b"", ts=None, nonce=None):
    ts = int(time.time() if ts is None else ts)
    nonce = nonce or secrets.token_hex(16)
    bh = hashlib.sha256(body).hexdigest()
    msg = auth.create_auth_message(method, target, bh, ts, nonce)
    return {"X-Auth-SS58Address": kp.ss58_address, "X-Auth-Signature": kp.sign(msg.encode()).hex(),
            "X-Auth-Message": msg, "X-Auth-Timestamp": str(ts), "X-Auth-Nonce": nonce,
            "X-Auth-Body-SHA256": bh, "Content-Type": "application/json"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth.auth_config, "enabled", True)
    monkeypatch.setattr(hotkey_whitelist, "_validators", [VALI.ss58_address, OTHER.ss58_address])
    monkeypatch.setattr(hotkey_whitelist, "ALLOW_MANUAL_HOTKEYS", False)
    auth.nonce_cache.clear()
    app = FastAPI()

    @app.post("/echo")
    async def echo(payload: dict, hk: str = Depends(get_validator_hotkey)):
        return {"hk": hk, "payload": payload}

    app.include_router(config_router.router)
    return TestClient(app)


def test_signed_request_is_accepted_once(client):
    body = json.dumps({"a": 1}).encode()
    h = _headers(VALI, "POST", "/echo", body)
    r = client.post("/echo", content=body, headers=h)
    assert r.status_code == 200 and r.json() == {"hk": VALI.ss58_address, "payload": {"a": 1}}
    assert client.post("/echo", content=body, headers=h).status_code == 401


def test_body_path_method_and_timestamp_are_bound(client):
    body = b'{"a":1}'
    assert client.post("/echo", content=b'{"a":2}', headers=_headers(VALI, "POST", "/echo", body)).status_code == 401
    assert client.post("/echo", content=body, headers=_headers(VALI, "POST", "/other", body)).status_code == 401
    assert client.post("/echo", content=body, headers=_headers(VALI, "PUT", "/echo", body)).status_code == 401
    stale = _headers(VALI, "POST", "/echo", body, ts=time.time() - 3600)
    assert client.post("/echo", content=body, headers=stale).status_code == 401


def test_nonce_cache_expires_in_order_and_caps_each_hotkey(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    cache = auth.NonceCache(per_hotkey=2)
    assert cache.add("a", "1", 10) and cache.add("a", "2", 10) and cache.add("b", "1", 10)
    assert not cache.add("a", "1", 10) and not cache.add("a", "3", 10)
    clock[0] = 111.0
    assert cache.add("a", "1", 10) and cache.add("a", "3", 10)
    assert len(cache._seen) == 2


@pytest.mark.parametrize("value,on", [("false", False), ("FALSE ", False), ("0", True),
                                      ("off", True), ("no", True), ("", True), ("true", True)])
def test_auth_is_off_only_on_an_explicit_false(monkeypatch, value, on):
    monkeypatch.setenv("AUTH_ENABLED", value)
    assert auth.AuthConfig().enabled is on


def test_auth_off_is_refused_outside_a_mock_network(monkeypatch):
    monkeypatch.setattr(auth.auth_config, "enabled", False)
    with pytest.raises(RuntimeError):
        auth.check_enabled(mock=False)
    auth.check_enabled(mock=True)
    monkeypatch.setattr(auth.auth_config, "enabled", True)
    auth.check_enabled(mock=False)
