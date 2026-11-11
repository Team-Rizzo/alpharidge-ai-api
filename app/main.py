"""Alpharidge AI API: app factory and middleware."""

import asyncio
import logging
from contextlib import asynccontextmanager

import bittensor  # noqa: F401  loaded before _setup_logging; see there
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import config, db
from app.routers import claims, dashboard, judge, ledger, mechanism, ops, product, scoops
from app.routers import config as config_router
from app.utils import auth, block, hotkey_whitelist
from app.utils.body_limit import BodyLimit
from app.utils.rate_limit import limit
from app.utils.receipt import ReceiptClock

logger = logging.getLogger(__name__)

_QUIET_PATHS = {"/health"}


def _is_v2(path: str) -> bool:
    return path == "/v2" or path.startswith("/v2/")


class _SuppressV2Log(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("/v2/" in message or '"/v2 ' in message)


class _SuppressBlockedHotkeyLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not config.BLOCKED_HOTKEYS:
            return True
        message = record.getMessage()
        return not any(hk in message for hk in config.BLOCKED_HOTKEYS)


class _SuppressForbiddenLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return '" 403 ' not in record.getMessage()


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    filters = [_SuppressV2Log(), _SuppressBlockedHotkeyLog(), _SuppressForbiddenLog()]
    access = logging.getLogger("uvicorn.access")
    targets = [access, *access.handlers, *logging.getLogger().handlers]
    for target in targets:
        for f in filters:
            if not any(isinstance(existing, type(f)) for existing in target.filters):
                target.addFilter(f)
    # bittensor silences loggers that exist when it is imported.
    for name in list(logging.root.manager.loggerDict):
        if name == "app" or name.startswith("app."):
            logging.getLogger(name).setLevel(logging.NOTSET)
    # httpx logs every request at INFO, including each Prisma query.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    blocked = _SuppressBlockedHotkeyLog()
    for name in ("app.utils.auth", "app.routers.auth"):
        logging.getLogger(name).addFilter(blocked)


class BlockedHotkeyMiddleware(BaseHTTPMiddleware):
    """Reject requests from BLOCKED_HOTKEYS before authentication, without logging."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in _QUIET_PATHS:
            if request.headers.get("X-Auth-SS58Address", "") in config.BLOCKED_HOTKEYS:
                return JSONResponse(status_code=status.HTTP_403_FORBIDDEN,
                                    content={"detail": "Access denied."})
        return await call_next(request)


class FilteredAccessLogMiddleware(BaseHTTPMiddleware):
    """Access log that skips 403s, /v2 and the high-frequency paths."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in _QUIET_PATHS or response.status_code == 403 or _is_v2(path):
            return response
        client_host = request.client.host if request.client else "-"
        logger.info(f'{client_host} - "{request.method} {path}" {response.status_code}')
        return response


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _profile(seconds: float) -> None:
    """Profile the whole process for a while after start-up and log where the time went."""
    import cProfile
    import io
    import pstats

    prof = cProfile.Profile()
    prof.enable()
    try:
        await asyncio.sleep(seconds)
    finally:
        prof.disable()
        out = io.StringIO()
        stats = pstats.Stats(prof, stream=out)
        stats.sort_stats("tottime").print_stats(40)
        stats.sort_stats("cumulative").print_stats(40)
        logger.info(f"[PROFILE] {seconds:.0f} s\n{out.getvalue()}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.jobs import answer_key
    from app.jobs.trust_nightly import run_daily
    from app.jobs.worker import run_worker
    from app.services import recheck, verifier

    logger.info("Starting Alpharidge AI API...")
    _setup_logging()
    # Refuse to start without the private settings.
    verifier.get_verifier()
    auth.check_enabled(block.is_mock())
    verifier.check_public_only(block.is_mock())
    recheck.check_proxy()
    if config.missing_private():
        raise RuntimeError(f"set {', '.join(config.missing_private())} (see .env.example)")
    await hotkey_whitelist.sync_async()
    await db.connect()

    tasks = [asyncio.create_task(hotkey_whitelist.run_sync()),
             asyncio.create_task(run_worker(claims.get_store())),
             asyncio.create_task(run_daily(claims.get_store())),
             asyncio.create_task(answer_key.run_forever(claims.get_store()))]
    if config.EVENT_LANE["PROFILE_S"] > 0:
        tasks.append(asyncio.create_task(_profile(config.EVENT_LANE["PROFILE_S"])))
    yield

    logger.info("Shutting down Alpharidge AI API...")
    for task in tasks:
        await _cancel(task)
    await db.disconnect()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Alpharidge AI API",
        description="Claims from miners, the signed points ledger for validators, and the "
                    "event feed",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if config.BLOCKED_HOTKEYS:
        app.add_middleware(BlockedHotkeyMiddleware)
    app.add_middleware(FilteredAccessLogMiddleware)
    app.add_middleware(BodyLimit)
    # Outermost, so the receipt time is taken before any other work.
    app.add_middleware(ReceiptClock)

    for module in (config_router, mechanism, ledger, ops, scoops, judge):
        app.include_router(module.router, dependencies=[Depends(limit("signed"))])
    for module in (dashboard, product):
        app.include_router(module.router, dependencies=[Depends(limit("public"))])
    app.include_router(claims.router, dependencies=[Depends(limit("claims"))])
    return app


app = create_app()
