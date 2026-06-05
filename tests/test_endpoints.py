import hotkey_whitelist


def test_is_miner_hotkey_uses_cached_list(monkeypatch):
    monkeypatch.setattr(hotkey_whitelist, "get_miner_hotkeys", lambda: ["minerA", "minerB"])
    assert hotkey_whitelist.is_miner_hotkey("minerA") is True
    assert hotkey_whitelist.is_miner_hotkey("not-a-miner") is False


import pytest
import verification as v
from utils import attestation_crypto as ac

from bittensor_wallet import Keypair  # sr25519 (default)


class _FakeVerdictTable:
    def __init__(self):
        self.rows = []

    async def upsert(self, where=None, data=None):
        self.rows.append(data["create"])


class _FakePrisma:
    def __init__(self):
        self.scoreverdict = _FakeVerdictTable()


@pytest.mark.asyncio
async def test_write_verdict_rejects_bad_miner_signature(monkeypatch):
    import main
    fake = _FakePrisma()
    monkeypatch.setattr(main, "prisma", fake, raising=False)
    monkeypatch.setattr(main.hotkey_whitelist, "is_miner_hotkey", lambda hk: True)

    written = await main.write_verdict(
        resource_type="tweet", resource_id="123", validator_hotkey="vali1",
        miner_hotkey="minerX", miner_signature="00", nonce="n1",
        miner_analysis_hash="abc", validator_verdict="valid",
        categorical_key="BTC|bull", points_awarded=1.0, epoch=7,
    )
    assert written is False
    assert fake.scoreverdict.rows == []


@pytest.mark.asyncio
async def test_write_verdict_writes_valid_signed_row(monkeypatch):
    import main
    fake = _FakePrisma()
    monkeypatch.setattr(main, "prisma", fake, raising=False)

    kp = Keypair.create_from_seed("0x" + "33" * 32)
    monkeypatch.setattr(main.hotkey_whitelist, "is_miner_hotkey", lambda hk: hk == kp.ss58_address)
    ah = "abc123"
    sig = kp.sign(ac.miner_sign_message("123", ah, "n1").encode("utf-8")).hex()

    written = await main.write_verdict(
        resource_type="tweet", resource_id="123", validator_hotkey="vali1",
        miner_hotkey=kp.ss58_address, miner_signature=sig, nonce="n1",
        miner_analysis_hash=ah, validator_verdict="valid",
        categorical_key="BTC|bull", points_awarded=5.0, epoch=7,
    )
    assert written is True
    assert len(fake.scoreverdict.rows) == 1
    row = fake.scoreverdict.rows[0]
    assert row["pointsAwarded"] == 1.0           # clamped to MAX_POINTS_PER_ITEM (default 1)
    assert row["minerHotkey"] == kp.ss58_address
    assert row["validatorVerdict"] == "valid"
    assert row["isAudit"] is False
    assert row["auditGroupId"] == "tweet:123:7"


def test_prod_safe_lever_defaults():
    # §2/§3/§4: every new lever must default to a no-op / non-restrictive value so a
    # plain deploy never changes subnet-wide behavior.
    import main
    assert main.MAX_OUTSTANDING_LEASES == 0      # unlimited (no lease throttle)
    assert main.AUDIT_OVERLAP_RATE == 0          # no silent audit re-leasing
    assert main.REPORTS_AUTO_BLACKLIST is False  # alarm-only
    assert main.VERDICT_ALLOWLIST_HOTKEYS == set()  # no allowlist restriction


@pytest.mark.asyncio
async def test_write_verdict_allowlist_gates_non_listed_validator(monkeypatch):
    import main
    fake = _FakePrisma()
    monkeypatch.setattr(main, "prisma", fake, raising=False)
    monkeypatch.setattr(main.hotkey_whitelist, "is_miner_hotkey", lambda hk: True)
    monkeypatch.setattr(main, "VERDICT_ALLOWLIST_HOTKEYS", {"valiX"}, raising=False)

    kp = Keypair.create_from_seed("0x" + "55" * 32)
    ah = "abc"
    sig = kp.sign(ac.miner_sign_message("9", ah, "n1").encode("utf-8")).hex()
    common = dict(resource_type="tweet", resource_id="9", miner_hotkey=kp.ss58_address,
                  miner_signature=sig, nonce="n1", miner_analysis_hash=ah,
                  validator_verdict="valid", categorical_key="BTC|bull",
                  points_awarded=1.0, epoch=7)

    # Not in the allowlist -> skipped, nothing written.
    assert await main.write_verdict(validator_hotkey="vali1", **common) is False
    assert fake.scoreverdict.rows == []
    # In the allowlist -> written.
    assert await main.write_verdict(validator_hotkey="valiX", **common) is True
    assert len(fake.scoreverdict.rows) == 1


@pytest.mark.asyncio
async def test_get_attestation_signs_budget(monkeypatch):
    import main, os
    os.environ["API_ATTESTATION_PRIVKEY"] = "44" * 32

    verdict_rows = [
        {"resource_type": "tweet", "resource_id": "1", "miner_hotkey": "m1",
         "validator_verdict": "valid", "categorical_key": "BTC|bull", "points_awarded": 1.0},
        {"resource_type": "tweet", "resource_id": "2", "miner_hotkey": "m1",
         "validator_verdict": "valid", "categorical_key": "ETH|bull", "points_awarded": 1.0},
        {"resource_type": "tweet", "resource_id": "3", "miner_hotkey": "m2",
         "validator_verdict": "invalid", "categorical_key": "X|y", "points_awarded": 1.0},
    ]

    class _VT:
        async def find_many(self, where=None):
            return [type("R", (), {
                "resourceType": r["resource_type"], "resourceId": r["resource_id"],
                "minerHotkey": r["miner_hotkey"], "validatorVerdict": r["validator_verdict"],
                "categoricalKey": r["categorical_key"], "pointsAwarded": r["points_awarded"],
            }) for r in verdict_rows]

    class _AT:
        async def upsert(self, where=None, data=None):
            return None

    class _P:
        scoreverdict = _VT()
        attestation = _AT()

    monkeypatch.setattr(main, "prisma", _P(), raising=False)

    resp = await main.get_attestation(epoch=7, validator_hotkey="vali1")
    assert resp.per_miner_points == {"m1": 2.0}     # m2's invalid verdict excluded
    assert resp.total_points == 2.0
    from utils import attestation_crypto as ac
    kp = ac.load_signing_key()
    msg = ac.attestation_message("vali1", 7, resp.per_miner_points, resp.total_points, resp.merkle_root)
    assert ac.verify_attestation(kp.ss58_address, msg, resp.signature) is True


@pytest.mark.asyncio
async def test_get_verdicts_returns_leaves_matching_attestation_root(monkeypatch):
    import main
    rows = [
        {"resource_type": "tweet", "resource_id": "1", "miner_hotkey": "m1",
         "validator_verdict": "valid", "categorical_key": "BTC|bull", "points_awarded": 1.0},
    ]

    class _VT:
        async def find_many(self, where=None):
            return [type("R", (), {
                "resourceType": r["resource_type"], "resourceId": r["resource_id"],
                "minerHotkey": r["miner_hotkey"], "validatorVerdict": r["validator_verdict"],
                "categoricalKey": r["categorical_key"], "pointsAwarded": r["points_awarded"],
            }) for r in rows]

    monkeypatch.setattr(main, "prisma", type("P", (), {"scoreverdict": _VT()}), raising=False)

    resp = await main.get_verdicts(validator="vali1", epoch=7, validator_hotkey="vali1")
    from utils import attestation_crypto as ac
    leaves = [l.model_dump() if hasattr(l, "model_dump") else l for l in resp.verdicts]
    recompute = ac.merkle_root([{
        "resource_type": l["resource_type"], "resource_id": l["resource_id"],
        "miner_hotkey": l["miner_hotkey"], "validator_verdict": l["validator_verdict"],
        "categorical_key": l["categorical_key"], "points_awarded": l["points_awarded"],
    } for l in leaves])
    assert recompute == ac.merkle_root(rows)
    assert resp.count == 1


@pytest.mark.asyncio
async def test_post_reports_alarm_only_by_default_and_blacklists_when_enabled(monkeypatch):
    import main

    state = {"reports": [], "blacklisted": []}

    class _RT:
        async def upsert(self, where=None, data=None):
            state["reports"].append(data["create"])

        async def find_many(self, where=None, distinct=None):
            rs = [r for r in state["reports"]
                  if r["accusedHotkey"] == where["accusedHotkey"] and r["epoch"] == where["epoch"]]
            seen, out = set(), []
            for r in rs:
                if r["reporterHotkey"] not in seen:
                    seen.add(r["reporterHotkey"])
                    out.append(type("R", (), r))
            return out

    class _BT:
        async def upsert(self, where=None, data=None):
            state["blacklisted"].append(where["hotkey"])

    class _P:
        broadcastreport = _RT()
        blacklistedhotkey = _BT()

    monkeypatch.setattr(main, "prisma", _P(), raising=False)
    from models import BroadcastReportCreate

    def report(reporter):
        return main.post_report(BroadcastReportCreate(
            accused_hotkey="bad", epoch=7, reason="budget_exceeded", evidence={}),
            validator_hotkey=reporter)

    # §3 default = alarm-only: even at consensus (2 distinct reporters), NO blacklist.
    monkeypatch.setattr(main, "REPORTS_AUTO_BLACKLIST", False, raising=False)
    await report("repA")
    assert state["blacklisted"] == []
    await report("repB")
    assert state["blacklisted"] == [], "alarm-only mode must not blacklist on consensus"

    # Operator enables enforcement: the same consensus now blacklists.
    monkeypatch.setattr(main, "REPORTS_AUTO_BLACKLIST", True, raising=False)
    await report("repB")  # consensus still 2 distinct reporters
    assert "bad" in state["blacklisted"]


@pytest.mark.asyncio
async def test_audit_divergence_for_group_flags_minority(monkeypatch):
    import main
    group_rows = [
        type("R", (), {"validatorHotkey": "A", "categoricalKey": "BTC|bull"}),
        type("R", (), {"validatorHotkey": "B", "categoricalKey": "BTC|bull"}),
        type("R", (), {"validatorHotkey": "C", "categoricalKey": "ETH|bear"}),
    ]

    class _VT:
        async def find_many(self, where=None):
            return group_rows

    monkeypatch.setattr(main, "prisma", type("P", (), {"scoreverdict": _VT()}), raising=False)
    diverged = await main.audit_divergence_for_group("tweet:9:7")
    assert diverged == ["C"]
