"""Request body size limit, checked before any route reads the body."""

from typing import Dict

BODY_MAX_BYTES = 64_000
# Routes whose bodies may be larger.
ROUTE_MAX_BYTES: Dict[str, int] = {"/ledger/reverify": 2_000_000, "/judge/verdicts": 128_000}


class _TooLarge(Exception):
    pass


def _too_large(e: BaseException) -> bool:
    """`_TooLarge`, alone or inside an exception group."""
    if isinstance(e, _TooLarge):
        return True
    sub = getattr(e, "subgroup", None)
    return callable(sub) and sub(_TooLarge) is not None


async def _reject(send) -> None:
    body = b'{"detail":"body_too_large"}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """Answers 413 when Content-Length or the streamed body passes the route's limit."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap = ROUTE_MAX_BYTES.get(scope.get("path", ""), BODY_MAX_BYTES)
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                if not value.isdigit() or int(value) > cap:
                    return await _reject(send)
        seen, started = 0, False

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    raise _TooLarge()
            return message

        async def tracked(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited, tracked)
        except Exception as e:
            if not _too_large(e):
                raise
            if not started:
                await _reject(send)
