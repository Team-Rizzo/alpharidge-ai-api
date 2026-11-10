"""Ledger and profile signing with the API's sr25519 key. The leaf, Merkle and message
formats are the subnet's own (`alpharidge_ai.ledger.attestation`), so validators verify
exactly what is signed here."""
from __future__ import annotations

import os

from bittensor_wallet import Keypair

from alpharidge_ai.ledger.attestation import (  # noqa: F401
    LEAF_V2, attestation_message, merkle_root, verify_attestation)


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
