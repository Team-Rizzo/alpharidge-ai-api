"""Signed mechanism profiles.

One signed, versioned object carries capacity and the ledger window. Validators resolve between
`current` and `next` by block height. Append-only: rolling back is publishing the old
body under a higher version.
"""

import logging
import time

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routers.auth import get_validator_hotkey
from app.utils import mechanism_profile

logger = logging.getLogger(__name__)
router = APIRouter()


class ErrorResponse(BaseModel):
    detail: str


class MechanismProfileResponse(BaseModel):
    """The profile in force and the one staged behind it, each signed.

    A validator resolves between them by block height, so both are served together and
    neither is described as active here.
    """
    current: Optional[dict] = None
    next: Optional[dict] = None


_PROFILE_CACHE_SECONDS = 60
_profile_cache: dict = {"at": 0.0, "body": None}


@router.get(
    "/mechanism/profile",
    response_model=MechanismProfileResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_mechanism_profile(validator_hotkey: str = Depends(get_validator_hotkey)):
    """Serve the signed profiles to validators only."""
    now = time.time()
    if _profile_cache["body"] is not None and (now - _profile_cache["at"]) < _PROFILE_CACHE_SECONDS:
        return MechanismProfileResponse(**_profile_cache["body"])

    try:
        payload = await mechanism_profile.load_profiles()
    except Exception as e:
        logger.error(f"Failed to load mechanism profiles: {e}")
        if _profile_cache["body"] is not None:
            return MechanismProfileResponse(**_profile_cache["body"])
        raise HTTPException(status_code=503, detail="profile unavailable")

    _profile_cache.update({"at": now, "body": payload})
    return MechanismProfileResponse(**payload)
