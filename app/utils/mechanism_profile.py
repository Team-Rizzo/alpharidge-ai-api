"""Mechanism profile: validation, signing and loading the published profiles, API side.

Bodies are parsed with the validators' own parser from the subnet package, so a profile
this side accepts is one the validators accept.

Signing reuses the attestation key validators already verify, so a profile needs no new
trust root.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from alpharidge_ai.mechanism import profile as subnet_profile
from app import db
from app.utils import attestation_crypto as ac
from app.utils import block as chain

SCHEMA_VERSION = subnet_profile.SCHEMA_VERSION
SECONDS_PER_BLOCK = subnet_profile.SECONDS_PER_BLOCK
signing_payload = subnet_profile.signing_payload
DEFAULT_REFRESH_SECONDS = 3600


class ProfileError(ValueError):
    """A profile that must not be published. The message is the reason."""


def min_lead_blocks(refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> int:
    return subnet_profile.min_lead_blocks(refresh_seconds)


def sign(raw: dict, keypair=None) -> str:
    keypair = keypair or ac.load_signing_key()
    return ac.sign_attestation(keypair, signing_payload(raw))


def validate(raw: dict, *, current_version: Optional[int] = None,
             refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> Dict[str, Any]:
    """Check a profile body. Raises ProfileError with the reason."""
    try:
        parsed = subnet_profile.parse(raw)
    except subnet_profile.ProfileError as e:
        raise ProfileError(str(e)) from None

    if current_version is not None and parsed.version <= int(current_version):
        raise ProfileError(
            f"version {parsed.version} not above the published {current_version}")

    lead = parsed.activation_block - parsed.publish_block
    required = min_lead_blocks(refresh_seconds)
    if lead < required:
        raise ProfileError(
            f"activation must lead publication by {required} blocks, got {lead}")
    return raw


def _row_body(row) -> dict:
    body = dict(row.body or {})
    body["signature"] = row.signature
    return body


async def load_profiles() -> dict:
    """The two profiles a validator needs: what is in force, and what is staged.

    Both are decided by block height on the validator, so this only splits the history
    into the newest published version and the one before it.
    """
    # Paged, not capped, so the profile in force is never dropped behind pending ones.
    block = await chain.current_block()
    if block is None:
        newest = await db.prisma.mechanismprofile.find_many(order={"version": "desc"},
                                                            take=1)
        return ({"current": _row_body(newest[0]), "next": None}
                if newest else {"current": None, "next": None})

    rows, cursor = [], None
    while True:
        page = await db.prisma.mechanismprofile.find_many(
            order={"version": "desc"}, take=100,
            **({"cursor": {"version": cursor}, "skip": 1} if cursor else {}))
        if not page:
            break
        rows.extend(page)
        # Descending, so the first activated row reached is the highest active one.
        if any(r.activationBlock <= block for r in page) or len(page) < 100:
            break
        cursor = page[-1].version

    if not rows:
        return {"current": None, "next": None}

    activated = [r for r in rows if r.activationBlock <= block]
    pending = [r for r in rows if r.activationBlock > block]
    current = max(activated, key=lambda r: r.version) if activated else None
    nxt = min(pending, key=lambda r: (r.activationBlock, r.version)) if pending else None
    return {"current": _row_body(current) if current else None,
            "next": _row_body(nxt) if nxt else None}
