"""
Shared crypto primitives for verifiable validator points.

CRITICAL: the canonical-JSON, Merkle, miner-signature, and attestation-message
formats here MUST match the validator-side mirror alpharidge_ai/utils/attestation_crypto.py
byte-for-byte, or offline signature/Merkle verification will fail across repos.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List

# sr25519 is the only signature scheme used here (API attestation key + miner hotkeys).
# bittensor_wallet's Keypair defaults to sr25519 and is the one library guaranteed present
# on the runtime stack (bittensor-wallet 4.0.1, no substrate-interface, no ed25519/KeypairType).
from bittensor_wallet import Keypair


def canonical_json(payload: dict) -> str:
    """Deterministic JSON: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _round(x) -> float:
    result = round(float(x), 6)
    return 0.0 if result == 0.0 else result  # collapse -0.0 for canonical stability


def analysis_hash(analysis: dict) -> str:
    """sha256 over canonical analysis dict (the miner's recorded output)."""
    return hashlib.sha256(canonical_json(analysis).encode("utf-8")).hexdigest()


# ---- Miner per-item signature -----------------------------------------------
def miner_sign_message(resource_id: str, analysis_hash_hex: str, nonce: str) -> str:
    """Message a miner signs (with its hotkey) for one scored item."""
    return f"alpharidge-miner-verdict:{resource_id}:{analysis_hash_hex}:{nonce}"


def verify_miner_signature(miner_hotkey: str, resource_id: str, analysis_hash_hex: str,
                           nonce: str, signature_hex: str) -> bool:
    try:
        msg = miner_sign_message(str(resource_id), analysis_hash_hex, nonce)
        kp = Keypair(ss58_address=miner_hotkey)  # miner hotkeys are sr25519 (default)
        return bool(kp.verify(msg.encode("utf-8"), bytes.fromhex(signature_hex)))
    except Exception:
        return False


# ---- Merkle tree over verdict leaves ----------------------------------------
# NOTE: leaves and internal nodes share sha256 with no domain separator. Second-preimage
# resistance relies on leaf content being schema-constrained (all six required fields).
# Do not relax leaf validation.
def _leaf_hash(verdict: dict) -> str:
    payload = canonical_json({
        "resource_type": verdict["resource_type"],
        "resource_id": str(verdict["resource_id"]),
        "miner_hotkey": verdict["miner_hotkey"],
        "validator_verdict": verdict["validator_verdict"],
        "categorical_key": verdict["categorical_key"],
        "points_awarded": _round(verdict["points_awarded"]),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merkle_root(verdicts: List[dict]) -> str:
    if not verdicts:
        return hashlib.sha256(b"").hexdigest()
    layer = sorted(_leaf_hash(v) for v in verdicts)
    while len(layer) > 1:
        nxt = []
        for i in range(0, len(layer), 2):
            left = layer[i]
            right = layer[i + 1] if i + 1 < len(layer) else layer[i]
            nxt.append(hashlib.sha256((left + right).encode("utf-8")).hexdigest())
        layer = nxt
    return layer[0]


# ---- Attestation signature (API sr25519 key) --------------------------------
def attestation_message(validator_hotkey: str, epoch: int, per_miner_points: Dict[str, float],
                        total_points: float, merkle_root_hex: str) -> str:
    return canonical_json({
        "validatorHotkey": validator_hotkey,
        "epoch": int(epoch),
        "perMinerPoints": {k: _round(v) for k, v in sorted(per_miner_points.items())},
        "totalPoints": _round(total_points),
        "merkleRoot": merkle_root_hex,
    })


def load_signing_key() -> "Keypair":
    """Load the API attestation sr25519 key from API_ATTESTATION_PRIVKEY (hex seed)."""
    seed_hex = os.getenv("API_ATTESTATION_PRIVKEY")
    if not seed_hex:
        raise RuntimeError("API_ATTESTATION_PRIVKEY not set")
    seed_hex = seed_hex[2:] if seed_hex.startswith("0x") else seed_hex
    if len(seed_hex) != 64:
        raise RuntimeError(
            f"API_ATTESTATION_PRIVKEY must be 64 hex chars (32 bytes), got {len(seed_hex)}"
        )
    return Keypair.create_from_seed("0x" + seed_hex)  # sr25519 (default)


def sign_attestation(keypair: "Keypair", message: str) -> str:
    return keypair.sign(message.encode("utf-8")).hex()


def verify_attestation(pubkey_ss58: str, message: str, signature_hex: str) -> bool:
    try:
        kp = Keypair(ss58_address=pubkey_ss58)  # sr25519 (default)
        return bool(kp.verify(message.encode("utf-8"), bytes.fromhex(signature_hex)))
    except Exception:
        return False
