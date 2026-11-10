"""sr25519 header authentication. The signed message is the subnet's
(`alpharidge_ai.ledger.request_auth`)."""

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from bittensor_wallet import Keypair
from fastapi import Request
from pydantic import BaseModel

from alpharidge_ai.ledger.request_auth import auth_message as create_auth_message
from alpharidge_ai.ledger.request_auth import body_sha256

logger = logging.getLogger(__name__)

_NONCE_MAX_LEN = 64


class AuthRequest(BaseModel):
    ss58_address: str
    signature: str
    message: str
    timestamp: float
    nonce: str = ""
    body_sha256: str = ""


class AuthConfig:
    def __init__(self):
        # Off only when set to exactly "false".
        self.enabled = os.getenv("AUTH_ENABLED", "true").strip().lower() != "false"
        self.signature_timeout = int(os.getenv("AUTH_SIGNATURE_TIMEOUT", "300"))


def check_enabled(mock: bool) -> None:
    """Refuse to run without request auth except on a mock network."""
    if auth_config.enabled:
        return
    if not mock:
        raise RuntimeError("AUTH_ENABLED=false is only allowed with SUBTENSOR_NETWORK=mock")
    logger.warning("[CONFIG] WARNING: AUTH_ENABLED=false, requests are not authenticated")


class NonceCache:
    """(hotkey, nonce) pairs seen within the signature window, oldest first."""

    def __init__(self, per_hotkey: Optional[int] = None):
        self.per_hotkey = per_hotkey if per_hotkey is not None else int(
            os.getenv("AUTH_NONCES_PER_HOTKEY", "5000"))
        self._seen: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._live: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._seen:
            key, exp = next(iter(self._seen.items()))
            if exp > now:
                return
            self._drop(key)

    def _drop(self, key: Tuple[str, str]) -> None:
        del self._seen[key]
        self._live[key[0]] -= 1
        if not self._live[key[0]]:
            del self._live[key[0]]

    def add(self, hotkey: str, nonce: str, ttl: float) -> bool:
        """Record a pair. Returns False if it was already seen and has not expired, or the
        hotkey already has its limit of live nonces."""
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            key = (hotkey, nonce)
            exp = self._seen.get(key)
            if exp is not None:
                if exp > now:
                    return False
                self._drop(key)
            if self._live.get(hotkey, 0) >= self.per_hotkey:
                return False
            self._seen[key] = now + ttl
            self._live[hotkey] = self._live.get(hotkey, 0) + 1
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
            self._live.clear()


def request_target(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def verify_signature(hotkey: str, signature_hex: str, message: str) -> bool:
    try:
        return Keypair(ss58_address=hotkey).verify(message.encode("utf-8"),
                                                   bytes.fromhex(signature_hex))
    except Exception as e:
        logger.error(f"Error verifying signature: {e}")
        return False


def _valid_nonce(nonce: str) -> bool:
    return 0 < len(nonce) <= _NONCE_MAX_LEN and all(c in "0123456789abcdef" for c in nonce)


def verify_auth_request(auth_request: AuthRequest, auth_config: AuthConfig, *,
                        method: str, target: str, body: bytes,
                        nonces: Optional["NonceCache"] = None) -> bool:
    try:
        if not auth_config.enabled:
            return True
        age = abs(time.time() - auth_request.timestamp)
        if age > auth_config.signature_timeout:
            logger.warning(f"Authentication timestamp outside window: {age:.0f}s")
            return False
        if not _valid_nonce(auth_request.nonce):
            return False
        body_hash = body_sha256(body)
        if auth_request.body_sha256 != body_hash:
            return False
        expected = create_auth_message(method, target, body_hash, auth_request.timestamp,
                                       auth_request.nonce)
        if auth_request.message != expected:
            logger.warning("Authentication message does not match the request")
            return False
        if not verify_signature(auth_request.ss58_address, auth_request.signature, expected):
            logger.warning(f"Invalid signature for {auth_request.ss58_address}")
            return False
        cache = nonce_cache if nonces is None else nonces
        if not cache.add(auth_request.ss58_address, auth_request.nonce,
                         2 * auth_config.signature_timeout):
            logger.warning(f"Reused nonce from {auth_request.ss58_address}")
            return False
        return True
    except Exception as e:
        logger.error(f"Error during authentication verification: {e}")
        return False


def extract_auth_from_headers(request: Request) -> Optional[AuthRequest]:
    try:
        h = request.headers
        ss58_address = h.get("X-Auth-SS58Address")
        signature = h.get("X-Auth-Signature")
        message = h.get("X-Auth-Message")
        timestamp = h.get("X-Auth-Timestamp")
        if not all([ss58_address, signature, message, timestamp]):
            return None
        return AuthRequest(ss58_address=ss58_address, signature=signature,
                           message=message, timestamp=float(timestamp),
                           nonce=h.get("X-Auth-Nonce", ""),
                           body_sha256=h.get("X-Auth-Body-SHA256", ""))
    except Exception as e:
        logger.error(f"Error extracting auth from headers: {e}")
        return None


auth_config = AuthConfig()
nonce_cache = NonceCache()
