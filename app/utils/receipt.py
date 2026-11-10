"""Receipt clock: the server's time for when a request body finished arriving."""

import time

from fastapi import Request


TIE_BUCKET_MS = 250

_KEY = "received_ns"


class ReceiptClock:
    """ASGI middleware that stamps the request once its last body chunk is read."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})

        async def stamped():
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                state.setdefault(_KEY, time.time_ns())
            return message

        await self.app(scope, stamped, send)


def received_ns(request: Request) -> int:
    """The stamp for a request whose body has been read."""
    return request.scope.get("state", {}).get(_KEY) or time.time_ns()


def to_ms(ns: int) -> int:
    return ns // 1_000_000


def bucket(received_ms: int) -> int:
    return received_ms // TIE_BUCKET_MS
