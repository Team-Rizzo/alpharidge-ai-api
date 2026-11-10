"""API-side profile validation and signing.

Bodies are parsed with the validators' parser, plus the publish-time version and lead
checks.
"""

import pytest

from app.utils import mechanism_profile as mp


def valid(version=2, publish_block=9_000, activation_block=10_000) -> dict:
    return {
        "version": version,
        "publish_block": publish_block,
        "activation_block": activation_block,
        "schema_version": "2.0.0",
        "settlement": {"C": 301_743.0},
        "ledger": {"window_epochs": 216},
    }


def test_a_valid_profile_passes():
    assert mp.validate(valid()) is not None


@pytest.mark.parametrize("section,key,bad", [
    ("settlement", "C", 0.0),
    ("ledger", "window_epochs", 0),
])
def test_out_of_range_values_are_refused(section, key, bad):
    body = valid()
    body[section][key] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(body)


def test_a_missing_section_is_refused():
    body = valid()
    del body["ledger"]
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(body)
    assert "ledger" in str(e.value)


def test_an_unsupported_schema_is_refused():
    body = valid()
    body["schema_version"] = "1.7.0"
    with pytest.raises(mp.ProfileError):
        mp.validate(body)


def test_a_version_that_does_not_advance_is_refused():
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(valid(version=4), current_version=4)
    assert "not above" in str(e.value)
    mp.validate(valid(version=5), current_version=4)


def test_too_short_a_lead_is_refused():
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(valid(publish_block=9_900, activation_block=10_000))
    assert "lead" in str(e.value)


def test_the_required_lead_covers_a_refresh_interval():
    assert mp.min_lead_blocks(3600) == 300


# ---- signing ----------------------------------------------------------------------

def test_the_signed_payload_excludes_the_signature():
    body = valid()
    body["signature"] = "deadbeef"
    assert "deadbeef" not in mp.signing_payload(body)


def test_a_signature_verifies_against_the_public_key():
    from bittensor_wallet import Keypair
    from app.utils import attestation_crypto as ac

    keypair = Keypair.create_from_seed("0x" + "11" * 32)
    body = valid()
    signature = mp.sign(body, keypair)
    assert ac.verify_attestation(keypair.ss58_address, mp.signing_payload(body),
                                 signature)


def test_a_tampered_body_fails_verification():
    from bittensor_wallet import Keypair
    from app.utils import attestation_crypto as ac

    keypair = Keypair.create_from_seed("0x" + "11" * 32)
    body = valid()
    signature = mp.sign(body, keypair)
    body["settlement"]["C"] = 999_999.0
    assert not ac.verify_attestation(keypair.ss58_address, mp.signing_payload(body),
                                     signature)


def test_ledger_params_come_from_the_profile():
    from app.services import ledger

    body = valid()
    p = ledger.params_from(body)
    assert (p.capacity, p.window) == (301_743.0, 216)
