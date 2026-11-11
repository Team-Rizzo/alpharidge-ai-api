"""Signed claim receipts: one per stored claim (`alpharidge_ai.ledger.receipts`)."""

from typing import Callable, Optional

from alpharidge_ai.ledger import receipts as rc
from app.services.store import ClaimRow
from app.utils import attestation_crypto as ac

_key = None


def _sign(text: str) -> str:
    global _key
    if _key is None:
        _key = ac.load_signing_key()
    return ac.sign_attestation(_key, text)


def builder(row: ClaimRow, sign: Callable[[str], str] = _sign
            ) -> Callable[[int, Optional[dict]], dict]:
    """The store calls this with the new claim's id and the last receipt stored."""
    def build(claim_id: int, prev: Optional[dict]) -> dict:
        return rc.make(seq=int(prev["seq"]) + 1 if prev else 1,
                       prev=prev["hash"] if prev else rc.GENESIS, claim_id=claim_id,
                       hotkey=row.hotkey, body_sha256=row.body_hash,
                       received_ms=row.received_ms, sign=sign)
    return build
