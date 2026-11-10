import pytest
from bittensor_wallet import Keypair  # sr25519 (default)

from app.utils import attestation_crypto as ac


def test_attestation_sign_verify_roundtrip():
    kp = Keypair.create_from_seed("0x" + "11" * 32)
    msg = ac.attestation_message("vali1", 7, {"m1": 3.0, "m2": 1.0}, 4.0, "deadbeef")
    sig = ac.sign_attestation(kp, msg)
    assert ac.verify_attestation(kp.ss58_address, msg, sig) is True
    assert ac.verify_attestation(kp.ss58_address, msg + "x", sig) is False
    kp2 = Keypair.create_from_seed("0x" + "33" * 32)
    assert ac.verify_attestation(kp2.ss58_address, msg, sig) is False


def test_load_signing_key_requires_env(monkeypatch):
    monkeypatch.delenv("API_ATTESTATION_PRIVKEY", raising=False)
    with pytest.raises(RuntimeError):
        ac.load_signing_key()
