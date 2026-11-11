"""A reader process serves only the read routes."""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app import config
from app import main as api_main


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(config, "API_ROLE", "reader")
    app = api_main.create_app()
    probe = APIRouter()

    @probe.get("/claims/{claim_id}")
    async def claim(claim_id: str):
        return {"id": claim_id}

    @probe.get("/dashboard/miners")
    async def miners():
        return {"ok": True}

    @probe.get("/judge/pending")
    async def pending():
        return {"items": []}

    @probe.post("/claims")
    async def submit():
        return {"ok": True}

    app.router.routes[:0] = probe.routes
    return TestClient(app)


def test_a_reader_answers_reads_and_turns_away_everything_else(reader):
    assert reader.get("/claims/12").status_code == 200
    assert reader.get("/dashboard/miners").status_code == 200
    for method, path in (("get", "/judge/pending"), ("post", "/claims"),
                         ("get", "/claims/12/x"), ("get", "/ops/alarms")):
        r = getattr(reader, method)(path)
        assert r.status_code == 404 and r.json() == {"detail": "writer_route"}
