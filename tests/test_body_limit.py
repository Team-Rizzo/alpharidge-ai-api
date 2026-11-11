from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.main import create_app
from app.utils import body_limit
from app.utils.body_limit import BodyLimit


def _client():
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(BodyLimit)
    return TestClient(app)


def test_a_body_past_the_limit_is_refused_before_the_route_reads_it():
    c = _client()
    assert c.post("/echo", content=b"x" * 1000).json() == {"n": 1000}
    big = b"x" * (body_limit.BODY_MAX_BYTES + 1)
    assert c.post("/echo", content=big).status_code == 413

    def chunks():
        for _ in range(100):
            yield b"x" * 1000

    assert c.post("/echo", content=chunks()).status_code == 413


def test_the_app_refuses_large_bodies_on_every_route():
    c = TestClient(create_app())
    r = c.put("/ops/blacklist/x", content=b"{" + b" " * 70_000 + b"}",
              headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_a_streamed_body_is_cut_off_behind_the_other_middleware():
    def chunks():
        for _ in range(100):
            yield b"x" * 1000

    c = TestClient(create_app())
    assert c.post("/claims", content=chunks()).status_code == 413


def test_a_streamed_body_is_refused_when_the_error_comes_back_grouped():
    try:
        group = ExceptionGroup
    except NameError:
        from exceptiongroup import ExceptionGroup as group

    async def grouped(scope, receive, send):
        try:
            while (await receive()).get("more_body"):
                pass
        except Exception as e:
            raise group("task group", [e])

    def chunks():
        for _ in range(100):
            yield b"x" * 1000

    assert TestClient(BodyLimit(grouped)).post("/x", content=chunks()).status_code == 413
