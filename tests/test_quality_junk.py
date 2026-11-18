"""Junk counts only for claims the page check and judge decided (judged_at set): a reject
before the page is read, such as novelty's `unsupported`, leaves quality as it is."""

import uuid

from alpharidge_ai.mechanism.quality import JUNK
from app.services import intake
from tests.test_store_contract import T0, claim, store  # noqa: F401 (fixture)


async def test_only_judged_rejects_count_as_junk(store):
    ck = "junk-ck-" + uuid.uuid4().hex[:8]
    hk = "junk-" + uuid.uuid4().hex[:10]
    ids = {}
    # (name, status before, reason, judged)
    for name, before, reason, judged in [
            ("novelty_unsupported", "queued", "unsupported", False),
            ("novelty_not_event", "queued", "not_an_event", False),
            ("judge_reject", "novelty", "judge_reject", True),
            ("scoop_stale", "novelty", "scoop_stale", True),
            ("resolve_unsupported", "fetched", "unsupported", True)]:
        row = await store.create_claim(claim(hk, coldkey=ck))
        assert await store.update_claim(row.id, "received", status=before)
        row = await store.get_claim(row.id)
        if before == "novelty":
            row = await intake.transition(store, row, "fetched")
        fields = {"judged_ms": T0} if judged else {"novelty": "known"}
        row = await intake.transition(store, row, "rejected", reason=reason, **fields)
        assert row.decided_ms is not None
        ids[name] = row.id
    paid = await store.create_claim(claim(hk, coldkey=ck))
    assert await store.update_claim(paid.id, "received", status="verified")
    await intake.transition(store, await store.get_claim(paid.id), "provisional")
    got = [(passed, cid) for owner, passed, cid in await store.quality_outcomes(0, JUNK)
           if owner == ck]
    assert got == [(False, ids["judge_reject"]), (False, ids["scoop_stale"]),
                   (False, ids["resolve_unsupported"]), (True, paid.id)]
