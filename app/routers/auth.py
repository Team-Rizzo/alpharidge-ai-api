"""Request dependencies: miner, validator and operator authentication, and the internal-IP guard."""

import logging
from typing import Callable, Optional

from fastapi import HTTPException, Request, status

from app import config
from app.utils import hotkey_whitelist
from app.utils.auth import (
    AuthRequest, auth_config, extract_auth_from_headers, request_target, verify_auth_request,
)

logger = logging.getLogger(__name__)


async def _authenticate(request: Request, allowed: Callable[[str], bool], role: str) -> str:
    auth_request = await authenticate(request, allowed, role)
    return auth_request.ss58_address if auth_request else "unauthenticated"


def no_query(request: Request) -> None:
    """Posts whose signature is checked again later against the bare path take no query."""
    if request.url.query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query_not_allowed")


async def authenticate(request: Request, allowed: Callable[[str], bool],
                       role: str) -> Optional[AuthRequest]:
    """Verify the signed headers for `role`. With auth disabled, the headers are taken as given."""
    auth_request = extract_auth_from_headers(request)
    if not auth_config.enabled:
        # Local/testing: attribute to the header hotkey when present.
        return auth_request

    if auth_request is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication headers. Required: X-Auth-SS58Address, "
                   "X-Auth-Signature, X-Auth-Message, X-Auth-Timestamp, X-Auth-Nonce, "
                   "X-Auth-Body-SHA256",
        )
    if not allowed(auth_request.ss58_address):
        logger.warning(f"Hotkey without {role} access: {auth_request.ss58_address}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Only {role}s are allowed to access this endpoint.",
        )
    body = await request.body()
    if not verify_auth_request(auth_request, auth_config, method=request.method,
                               target=request_target(request), body=body):
        logger.warning(f"Authentication failed for hotkey: {auth_request.ss58_address}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication. Signature verification failed.",
        )
    return auth_request


async def get_validator_hotkey(request: Request) -> str:
    """Authenticate the caller as a validator and return its hotkey."""
    return await _authenticate(request, hotkey_whitelist.is_validator_hotkey, "validator")


async def get_operator_hotkey(request: Request) -> str:
    """Authenticate the caller as an operator (OPERATOR_HOTKEYS) and return its hotkey."""
    hotkey = await _authenticate(request, lambda hk: hk in config.OPERATOR_HOTKEYS, "operator")
    if not auth_config.enabled and hotkey not in config.OPERATOR_HOTKEYS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Access denied. Only operators are allowed to access this endpoint.")
    return hotkey


async def require_internal(request: Request) -> None:
    """Allow only localhost and INTERNAL_ALLOWED_IPS."""
    client_ip = request.client.host if request.client else None
    if client_ip not in config.INTERNAL_ALLOWED_IPS:
        raise HTTPException(status_code=403, detail="Access restricted.")
