"""One-off: generate the API attestation ed25519 keypair.

Run:  python scripts/gen_attestation_key.py
Set the printed PRIVKEY (hex seed) as API_ATTESTATION_PRIVKEY on the API.
Pin the printed PUBKEY (ss58) as API_ATTESTATION_PUBKEY in every validator config.
"""
import secrets

try:
    from bittensor_wallet import Keypair, KeypairType
except ImportError:
    from substrateinterface import Keypair, KeypairType

seed = secrets.token_hex(32)
kp = Keypair.create_from_seed("0x" + seed, crypto_type=KeypairType.ED25519)
print("API_ATTESTATION_PRIVKEY=" + seed)
print("API_ATTESTATION_PUBKEY=" + kp.ss58_address)
