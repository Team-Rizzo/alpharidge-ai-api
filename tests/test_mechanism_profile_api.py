"""API-side profile validation and signing.

The range checks here must agree with the validator's. A profile this side accepts and
the fleet rejects is a published mechanism nobody is running.
"""

import json

import pytest

from utils import mechanism_profile as mp


def valid(version=2, publish_block=9_000, activation_block=10_000) -> dict:
    return {
        "version": version,
        "publish_block": publish_block,
        "activation_block": activation_block,
        "schema_version": "1.2.0",
        "settlement": {"C": 301_743.0},
        "emission": {"midpoint": 0.766, "gain": 15.0, "ceiling": 1.0,
                     "bonus_start": 0.716, "bonus_full": 0.816,
                     "n_min": 100, "ema_alpha": 0.03},
        "rations": {"explore": 25.0, "probe_day": 2.0, "alpha_day": 0.5,
                    "cap": 5000.0, "slack_target": 0.15, "fill_gate": 0.97,
                    "boost": 200.0, "boost_days": 14, "boost_tranche_max": 0.05},
        "oracle": {"pool_tiers": ["number_bearing"], "keyed_rate_pool": 0.9,
                   "keyed_rate_keeper": 0.03, "claim_cap": 40, "keeper_weight": 0.3,
                   "grader_models": [{"id": "model-a", "weight": 0.7},
                                     {"id": "model-b", "weight": 0.3}],
                   "schema_cutover_block": 12_000},
        "controller": {"roi_lo": 1.5, "roi_hi": 6.0, "arm_days": 3,
                       "max_step": 0.2, "gap_days": 14, "cost_per_point": 0.000292},
    }


def test_a_valid_profile_passes():
    assert mp.validate(valid()) is not None


@pytest.mark.parametrize("section,key,bad", [
    ("emission", "gain", 201.0),
    ("emission", "ceiling", 20.5),
    ("oracle", "keeper_weight", 100.5),
    ("settlement", "C", 0.0),
    ("oracle", "keyed_rate_pool", 1.5),
    ("controller", "max_step", 0.0),
    ("rations", "probe_day", 0.5),
])
def test_out_of_range_values_are_refused(section, key, bad):
    body = valid()
    body[section][key] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(body)


@pytest.mark.parametrize("section,key,edge", [
    ("emission", "gain", 200.0),
    ("emission", "ceiling", 20.0),
    ("oracle", "keeper_weight", 100.0),
])
def test_values_at_the_bound_are_accepted(section, key, edge):
    """The bound itself is publishable: a profile has to be able to express the
    top of each range, or the widening does not reach the curve."""
    body = valid()
    body[section][key] = edge
    assert mp.validate(body) is not None


def test_a_missing_section_is_refused():
    body = valid()
    del body["controller"]
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(body)
    assert "controller" in str(e.value)


def test_an_unsupported_schema_is_refused():
    body = valid()
    body["schema_version"] = "2.0.0"
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


def test_the_signed_payload_is_key_order_independent():
    body = valid()
    shuffled = {k: body[k] for k in reversed(list(body))}
    assert mp.signing_payload(body) == mp.signing_payload(shuffled)


def test_the_signed_payload_matches_the_validators_encoding():
    """Both sides sign the same bytes, or every profile is rejected fleet-wide."""
    body = valid()
    expected = json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    assert mp.signing_payload(body) == expected


def test_a_signature_verifies_against_the_public_key():
    from bittensor_wallet import Keypair
    from utils import attestation_crypto as ac

    keypair = Keypair.create_from_seed("0x" + "11" * 32)
    body = valid()
    signature = mp.sign(body, keypair)
    assert ac.verify_attestation(keypair.ss58_address, mp.signing_payload(body),
                                 signature)


def test_a_tampered_body_fails_verification():
    from bittensor_wallet import Keypair
    from utils import attestation_crypto as ac

    keypair = Keypair.create_from_seed("0x" + "11" * 32)
    body = valid()
    signature = mp.sign(body, keypair)
    body["settlement"]["C"] = 999_999.0
    assert not ac.verify_attestation(keypair.ss58_address, mp.signing_payload(body),
                                     signature)


def valid_13() -> dict:
    raw = valid()
    raw["schema_version"] = "1.3.0"
    return raw


def test_channel_weights_are_optional():
    raw = valid()
    assert "channel_weights" not in raw["emission"]
    assert mp.validate(raw) is not None


def test_named_channel_weights_pass():
    raw = valid_13()
    raw["emission"]["channel_weights"] = {"audit": 4.0, "legacy": 0.0}
    assert mp.validate(raw) is not None


@pytest.mark.parametrize("bad", [
    {"nonsense": 1.0},
    {"audit": -1.0},
    {"audit": 101.0},
    {name: 0.0 for name in mp.REPUTATION_CHANNELS},
    [1, 2],
])
def test_bad_channel_weights_are_refused(bad):
    raw = valid_13()
    raw["emission"]["channel_weights"] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)


def test_grader_model_scales_pass():
    raw = valid_13()
    raw["oracle"]["grader_models"][0]["scale"] = 0.8
    raw["oracle"]["grader_models"][1]["keeper_scale"] = 0.9
    assert mp.validate(raw) is not None


@pytest.mark.parametrize("field", ["scale", "keeper_scale"])
@pytest.mark.parametrize("bad", [0.0, -0.1, 1.01, "x"])
def test_bad_grader_model_scales_are_refused(field, bad):
    raw = valid_13()
    raw["oracle"]["grader_models"][0][field] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)


def test_channel_alphas_pass():
    raw = valid_13()
    raw["emission"]["channel_alphas"] = {"audit": 0.015}
    assert mp.validate(raw) is not None


@pytest.mark.parametrize("bad", [{"nonsense": 0.1}, {"audit": 0.0}, {"audit": 1.5}, [0.1]])
def test_bad_channel_alphas_are_refused(bad):
    raw = valid_13()
    raw["emission"]["channel_alphas"] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)



@pytest.mark.parametrize("path, field, value", [
    ("emission", "channel_weights", {"audit": 3.0}),
    ("emission", "channel_alphas", {"audit": 0.015}),
    ("model", "scale", 0.9),
    ("model", "keeper_scale", 0.9),
])
def test_new_fields_need_the_new_schema(path, field, value):
    raw = valid()
    target = raw["emission"] if path == "emission" else raw["oracle"]["grader_models"][0]
    target[field] = value
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)
    raw["schema_version"] = "1.3.0"
    assert mp.validate(raw) is not None


def test_a_plain_profile_passes_under_either_schema():
    assert mp.validate(valid()) is not None
    assert mp.validate(valid_13()) is not None


def valid_14() -> dict:
    raw = valid()
    raw["schema_version"] = "1.4.0"
    return raw


def test_channel_defaults_need_schema_1_4():
    for version in ("1.2.0", "1.3.0"):
        raw = valid()
        raw["schema_version"] = version
        raw["emission"]["channel_defaults"] = {"audit": 0.34}
        with pytest.raises(mp.ProfileError):
            mp.validate(raw)
    raw = valid_14()
    raw["emission"]["channel_defaults"] = {"audit": 0.34, "triage": 0.97, "floor": 0.93}
    raw["emission"]["channel_weights"] = {"audit": 3.0}
    raw["oracle"]["grader_models"][0]["scale"] = 0.9
    assert mp.validate(raw) is not None


@pytest.mark.parametrize("bad", [{"nonsense": 0.5}, {"audit": -0.1}, {"audit": 1.1}, [0.5]])
def test_bad_channel_defaults_are_refused(bad):
    raw = valid_14()
    raw["emission"]["channel_defaults"] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)


def valid_15() -> dict:
    raw = valid()
    raw["schema_version"] = "1.5.0"
    return raw


@pytest.mark.parametrize("field,value", [("channel_weights", 3.0), ("channel_alphas", 0.03),
                                         ("channel_defaults", 0.4)])
def test_the_audit_v2_channel_needs_schema_1_5(field, value):
    raw = valid_14()
    raw["emission"][field] = {"audit_v2": value}
    with pytest.raises(mp.ProfileError, match="1.5.0"):
        mp.validate(raw)
    raw = valid_15()
    raw["emission"][field] = {"audit_v2": value}
    assert mp.validate(raw) is not None


def test_the_audit_v2_scale_needs_schema_1_5():
    raw = valid_14()
    raw["oracle"]["grader_models"][0]["audit_v2_scale"] = 0.8
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)
    raw["schema_version"] = "1.5.0"
    assert mp.validate(raw) is not None


@pytest.mark.parametrize("bad", [0.0, 1.5, -0.2])
def test_a_bad_audit_v2_scale_is_refused(bad):
    raw = valid_15()
    raw["oracle"]["grader_models"][0]["audit_v2_scale"] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(raw)


def test_schema_1_5_accepts_the_earlier_fields_and_the_swap():
    raw = valid_15()
    raw["emission"]["channel_weights"] = {"audit": 0.0, "audit_v2": 3.0, "triage": 1.0}
    raw["emission"]["channel_defaults"] = {"audit_v2": 0.4, "triage": 0.97}
    raw["oracle"]["grader_models"][0].update(scale=0.95, keeper_scale=1.0,
                                             audit_v2_scale=0.83)
    assert mp.validate(raw) is not None
