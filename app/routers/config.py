"""Health, subnet config and validator versions."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app import config
from app.routers.auth import get_validator_hotkey, require_internal

logger = logging.getLogger(__name__)
router = APIRouter()

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/v2", methods=_ALL_METHODS)
@router.api_route("/v2/{path:path}", methods=_ALL_METHODS)
async def v2_catchall(request: Request, path: str = ""):
    """Unauthenticated, so legacy callers get an upgrade message rather than an auth error."""
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "error": "deprecated_api_version",
            "message": "The /v2 API is deprecated. Please update your code.",
            "requested_path": request.url.path,
            "method": request.method,
        },
        headers={"Deprecation": "true"},
    )


@router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# Latest version each validator reported on its config poll, persisted across restarts.
_VERSIONS_PATH = Path(config.VALIDATOR_VERSIONS_PATH)


def _load_validator_versions() -> dict:
    try:
        if _VERSIONS_PATH.exists():
            return json.loads(_VERSIONS_PATH.read_text())
    except Exception as e:
        logger.warning(f"Failed to load validator versions from {_VERSIONS_PATH}: {e}")
    return {}


def _save_validator_versions() -> None:
    try:
        tmp = _VERSIONS_PATH.with_name(_VERSIONS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_validator_versions, indent=2))
        tmp.replace(_VERSIONS_PATH)
    except Exception as e:
        logger.warning(f"Failed to save validator versions to {_VERSIONS_PATH}: {e}")


_validator_versions: dict = _load_validator_versions()


@router.get("/config/subnet")
async def get_subnet_config(
    request: Request,
    validator_hotkey: str = Depends(get_validator_hotkey),
):
    """Served validator settings and the minimum validator version; records the version the
    caller reports."""
    client_version = request.headers.get("X-Validator-Version", "unknown")
    logger.info(f"Config request from {validator_hotkey[:12]}.. version={client_version}")

    now = datetime.now(timezone.utc).isoformat()
    rec = _validator_versions.get(validator_hotkey)
    if rec is None:
        _validator_versions[validator_hotkey] = {
            "version": client_version, "first_seen": now, "last_seen": now, "poll_count": 1,
        }
    else:
        rec["version"] = client_version
        rec["last_seen"] = now
        rec["poll_count"] = rec.get("poll_count", 0) + 1
    _save_validator_versions()

    return {
        "config": config.validator_config(validator_hotkey),
        "min_validator_version": config.MIN_VALIDATOR_VERSION,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/validators/versions", dependencies=[Depends(require_internal)])
async def get_validator_versions():
    """Latest validator versions seen on config polls, most recent first."""
    validators = [
        {"hotkey": hk, **rec}
        for hk, rec in sorted(_validator_versions.items(),
                              key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
    ]
    return {
        "validators": validators,
        "count": len(validators),
        "min_validator_version": config.MIN_VALIDATOR_VERSION,
        "timestamp": datetime.utcnow().isoformat(),
    }
