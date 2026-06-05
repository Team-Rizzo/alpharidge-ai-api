import pytest

from utils import attestation_crypto as ac

from bittensor_wallet import Keypair  # sr25519 (default)


def test_canonical_json_is_sorted_and_compact():
    out = ac.canonical_json({"b": 1, "a": 2})
    assert out == '{"a":2,"b":1}'


def test_merkle_root_empty_is_stable():
    assert ac.merkle_root([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert len(ac.merkle_root([])) == 64


def test_merkle_root_is_order_independent():
    v1 = {"resource_type": "tweet", "resource_id": "1", "miner_hotkey": "m1",
          "validator_verdict": "valid", "categorical_key": "BTC|bull", "points_awarded": 1.0}
    v2 = {"resource_type": "tweet", "resource_id": "2", "miner_hotkey": "m2",
          "validator_verdict": "valid", "categorical_key": "ETH|bear", "points_awarded": 1.0}
    assert ac.merkle_root([v1, v2]) == ac.merkle_root([v2, v1])


def test_merkle_root_changes_when_a_leaf_changes():
    v1 = {"resource_type": "tweet", "resource_id": "1", "miner_hotkey": "m1",
          "validator_verdict": "valid", "categorical_key": "BTC|bull", "points_awarded": 1.0}
    v1b = dict(v1, points_awarded=2.0)
    assert ac.merkle_root([v1]) != ac.merkle_root([v1b])


def test_attestation_sign_verify_roundtrip():
    kp = Keypair.create_from_seed("0x" + "11" * 32)
    msg = ac.attestation_message("vali1", 7, {"m1": 3.0, "m2": 1.0}, 4.0, "deadbeef")
    sig = ac.sign_attestation(kp, msg)
    assert ac.verify_attestation(kp.ss58_address, msg, sig) is True
    assert ac.verify_attestation(kp.ss58_address, msg + "x", sig) is False
    kp2 = Keypair.create_from_seed("0x" + "33" * 32)
    assert ac.verify_attestation(kp2.ss58_address, msg, sig) is False


def test_miner_signature_roundtrip():
    kp = Keypair.create_from_seed("0x" + "22" * 32)
    analysis = {"sentiment": "bull", "asset_symbol": "BTC"}
    ah = ac.analysis_hash(analysis)
    msg = ac.miner_sign_message("123", ah, "nonce-abc")
    sig = kp.sign(msg.encode("utf-8")).hex()
    assert ac.verify_miner_signature(kp.ss58_address, "123", ah, "nonce-abc", sig) is True
    assert ac.verify_miner_signature(kp.ss58_address, "123", ah, "other-nonce", sig) is False


def test_miner_sign_message_format():
    assert ac.miner_sign_message("r1", "ab12", "n1") == "talisman-miner-verdict:r1:ab12:n1"


def test_load_signing_key_requires_env(monkeypatch):
    import pytest as _pytest
    monkeypatch.delenv("API_ATTESTATION_PRIVKEY", raising=False)
    with _pytest.raises(RuntimeError):
        ac.load_signing_key()


def test_negative_zero_points_canonicalize_to_zero():
    a = ac.attestation_message("v", 1, {"m": -0.0}, -0.0, "r")
    b = ac.attestation_message("v", 1, {"m": 0.0}, 0.0, "r")
    assert a == b
