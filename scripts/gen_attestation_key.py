"""One-off: generate the API attestation sr25519 keypair.

Run:  python scripts/gen_attestation_key.py
Set the printed PRIVKEY (hex seed) as API_ATTESTATION_PRIVKEY on the API.
Pin the printed PUBKEY (ss58) as API_ATTESTATION_PUBKEY in every validator config.

sr25519 is used (not ed25519) because it is the only signature scheme available on
the runtime stack (bittensor-wallet 4.0.1, no substrate-interface / no KeypairType).
"""
import secrets

from bittensor_wallet import Keypair

seed = secrets.token_hex(32)
kp = Keypair.create_from_seed("0x" + seed)  # sr25519 (default)
print("API_ATTESTATION_PRIVKEY=" + seed)
print("API_ATTESTATION_PUBKEY=" + kp.ss58_address)
