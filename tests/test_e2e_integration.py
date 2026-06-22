"""
Opt-in end-to-end integration test for Verifiable Validator Point Broadcasting.

Drives the REAL FastAPI app (in-process via httpx ASGITransport) against a REAL
Postgres schema, with REAL sr25519 miner signatures and a REAL sr25519 API
attestation key, then exercises the REAL validator-side modules (offline attestation
ingest, deep-verify, reports). No mocks for the trust spine.

Gated behind E2E=1 so normal CI is unaffected (it needs a live Postgres).

Run:
    E2E=1 python -m pytest tests/test_e2e_integration.py -v

Config (env overrides):
    E2E_DATABASE_URL   default postgresql://talisman:talisman_dev@127.0.0.1:5433/talisman?schema=talisman_e2e
    API_ATTESTATION_PRIVKEY   default: a fresh random sr25519 seed
"""
import os
import sys
import secrets
import shutil
import subprocess

import pytest

E2E = os.environ.get("E2E") == "1"
if not E2E:
    pytest.skip("set E2E=1 to run DB integration tests", allow_module_level=True)

# --- env MUST be set before importing the API app ---------------------------
SCHEMA = os.environ.get("E2E_SCHEMA", "talisman_e2e")
DATABASE_URL = os.environ.get(
    "E2E_DATABASE_URL",
    f"postgresql://talisman:talisman_dev@127.0.0.1:5433/talisman?schema={SCHEMA}",
)
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["AUTH_ENABLED"] = "false"
os.environ.setdefault("BLACKLISTED_HOTKEY_PREFIXES", "")  # don't blacklist synthetic keys
os.environ.setdefault("AUDIT_OVERLAP_RATE", "0")          # deterministic leasing
os.environ.setdefault("SCORING_LEASE_TTL_SECONDS", "900")
os.environ.setdefault("API_ATTESTATION_PRIVKEY", secrets.token_hex(32))

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAL_DIR = os.path.abspath(os.path.join(API_DIR, "..", "alpharidge-ai"))
sys.path.insert(0, VAL_DIR)
sys.path.insert(0, API_DIR)

import pytest_asyncio  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402

from bittensor_wallet import Keypair  # sr25519 (default)

import main                                    # noqa: E402
import hotkey_whitelist                         # noqa: E402
from utils import attestation_crypto as api_ac  # noqa: E402

from alpharidge_ai.utils import attestation_crypto as val_ac          # noqa: E402
from alpharidge_ai.validator import deep_verify                       # noqa: E402
from alpharidge_ai.validator.reward_broadcast_store import (          # noqa: E402
    RewardBroadcastStore, route_reward_broadcast, MAX_POINTS_PER_UID,
)

# ----------------------------------------------------------------------------
# Synthetic actors / fixtures
# ----------------------------------------------------------------------------
ATT_KP = Keypair.create_from_seed(
    "0x" + os.environ["API_ATTESTATION_PRIVKEY"])
PINNED_PUBKEY = ATT_KP.ss58_address

MINER1 = Keypair.create_from_seed("0x" + "a1" * 32)
MINER2 = Keypair.create_from_seed("0x" + "b2" * 32)
VALI1 = Keypair.create_from_seed("0x" + "c3" * 32)
VALI2 = Keypair.create_from_seed("0x" + "d4" * 32)
VALI3 = Keypair.create_from_seed("0x" + "e5" * 32)
MINERS = {MINER1.ss58_address: MINER1, MINER2.ss58_address: MINER2}
EPOCH = 4242


class Analysis:
    """Minimal carrier with the six V3 categorical fields."""
    def __init__(self, sentiment, asset_symbol, content_type,
                 technical_quality, market_analysis, impact_potential):
        self.sentiment = sentiment
        self.asset_symbol = asset_symbol
        self.content_type = content_type
        self.technical_quality = technical_quality
        self.market_analysis = market_analysis
        self.impact_potential = impact_potential


def vheaders(kp):
    # Auth disabled, but identity is only attributed when all four auth headers are
    # present (signatures NOT verified in this mode); version clears the lease gate.
    return {
        "X-Auth-SS58Address": kp.ss58_address,
        "X-Auth-Signature": "00",
        "X-Auth-Message": "e2e",
        "X-Auth-Timestamp": "0",
        "X-Validator-Version": "9.9.9",
    }


def sign_completed(miner_kp, tweet_id, analysis, verdict="valid"):
    ah = val_ac.analysis_hash(val_ac.analysis_to_dict(analysis))
    cat = val_ac.categorical_key(analysis)
    nonce = f"nonce-{tweet_id}"
    sig = val_ac.sign_miner_item(miner_kp, str(tweet_id), ah, nonce)
    return {
        "tweet_id": tweet_id, "sentiment": analysis.sentiment,
        "asset_symbol": analysis.asset_symbol, "content_type": analysis.content_type,
        "epoch": EPOCH, "miner_hotkey": miner_kp.ss58_address, "miner_signature": sig,
        "nonce": nonce, "miner_analysis_hash": ah, "validator_verdict": verdict,
        "categorical_key": cat, "points_awarded": 1.0,
    }


async def _reset_db():
    for tbl in ("score_verdict", "attestation", "broadcast_report",
                "tweet_analysis", "scoring", "tweets", "blacklisted_hotkeys",
                # News tables: delete children first, then articles.
                "news_article_scoring", "news_article_analysis", "news_articles"):
        await main.prisma.execute_raw(f'DELETE FROM "{SCHEMA}"."{tbl}";')


async def _seed_tweets(ids):
    from datetime import datetime, timezone
    for tid in ids:
        await main.prisma.tweet.create(data={
            "id": tid, "text": f"Synthetic tweet {tid} about $BTC.",
            "createdAt": datetime.now(timezone.utc)})


@pytest_asyncio.fixture
async def client():
    """Push the schema into an isolated namespace, connect Prisma, reset, yield client."""
    prisma_bin = shutil.which("prisma") or os.path.join(os.path.dirname(sys.executable), "prisma")
    subprocess.run(
        [prisma_bin, "db", "push", "--skip-generate", "--accept-data-loss"],
        cwd=API_DIR, env={**os.environ}, check=True, capture_output=True)
    hotkey_whitelist.get_miner_hotkeys = lambda: list(MINERS.keys())
    await main.prisma.connect()
    await _reset_db()
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://e2e") as c:
        yield c
    await main.prisma.disconnect()


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------
def test_cross_repo_crypto_parity():
    """The whole system depends on byte-identical crypto across both repos."""
    leaf = {"resource_type": "tweet", "resource_id": "9", "miner_hotkey": MINER1.ss58_address,
            "validator_verdict": "valid", "categorical_key": "bull|BTC|news||up|high",
            "points_awarded": 1.0}
    assert api_ac.merkle_root([leaf]) == val_ac.merkle_root([leaf])
    pm = {MINER1.ss58_address: 3.0, MINER2.ss58_address: 1.0}
    assert (api_ac.attestation_message(VALI1.ss58_address, EPOCH, pm, 4.0, "ab"*32)
            == val_ac.attestation_message(VALI1.ss58_address, EPOCH, pm, 4.0, "ab"*32))


async def test_full_integration_flow(client):
    # --- 1. Lease -> sign -> submit -> verdicts (Validator 1) ---
    t_ids = [1001, 1002, 1003]
    await _seed_tweets(t_ids)
    r = await client.get("/tweets/unscored", params={"limit": 3}, headers=vheaders(VALI1))
    assert r.status_code == 200, r.text
    leased_ids = [t["id"] for t in r.json()["tweets"]]
    assert len(leased_ids) == 3

    analyses = {
        leased_ids[0]: Analysis("bull", "BTC", "news", "high", "breakout", "high"),
        leased_ids[1]: Analysis("bear", "ETH", "opinion", "low", "rejection", "medium"),
        leased_ids[2]: Analysis("neutral", "SOL", "news", "medium", "range", "low"),
    }
    completed = [sign_completed(MINER1, leased_ids[0], analyses[leased_ids[0]]),
                 sign_completed(MINER1, leased_ids[1], analyses[leased_ids[1]]),
                 sign_completed(MINER2, leased_ids[2], analyses[leased_ids[2]])]
    r = await client.post("/tweets/completed",
                          json={"completed_tweets": completed}, headers=vheaders(VALI1))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3

    vrows = await main.prisma.scoreverdict.find_many(
        where={"validatorHotkey": VALI1.ss58_address, "epoch": EPOCH})
    assert len(vrows) == 3
    assert all(row.pointsAwarded <= 1.0 for row in vrows)  # clamped to MAX_POINTS_PER_ITEM

    r = await client.get("/verdicts",
                         params={"validator": VALI1.ss58_address, "epoch": EPOCH},
                         headers=vheaders(VALI2))
    assert r.status_code == 200 and r.json()["count"] == 3

    # --- 2. Signed attestation + offline verification ---
    r = await client.get("/attestation", params={"epoch": EPOCH}, headers=vheaders(VALI1))
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["total_points"] == 3.0
    assert att["per_miner_points"].get(MINER1.ss58_address) == 2.0
    assert att["per_miner_points"].get(MINER2.ss58_address) == 1.0

    attestation_obj = {
        "validatorHotkey": att["validator_hotkey"], "epoch": int(att["epoch"]),
        "perMinerPoints": att["per_miner_points"], "totalPoints": att["total_points"],
        "merkleRoot": att["merkle_root"]}
    att_sig = att["signature"]
    msg = val_ac.attestation_message(
        attestation_obj["validatorHotkey"], attestation_obj["epoch"],
        attestation_obj["perMinerPoints"], attestation_obj["totalPoints"],
        attestation_obj["merkleRoot"])
    assert val_ac.verify_attestation(PINNED_PUBKEY, msg, att_sig)
    assert not val_ac.verify_attestation(VALI2.ss58_address, msg, att_sig)  # wrong key

    hotkey_to_uid = {MINER1.ss58_address: 10, MINER2.ss58_address: 11}
    store = RewardBroadcastStore(path="/tmp/e2e_pytest_recv.json")
    store.last_seen_seq.clear(); store.by_epoch_by_sender.clear()
    store.merkle_by_epoch_by_sender.clear()
    ok, why = route_reward_broadcast(
        store=store, sender_hotkey=VALI1.ss58_address, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation=attestation_obj, attestation_sig=att_sig,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert ok, why
    agg = store.aggregate_epoch(EPOCH)
    assert agg.get(10) == 2 and agg.get(11) == 1
    assert store.get_merkle_root(epoch=EPOCH, sender=VALI1.ss58_address) == attestation_obj["merkleRoot"]

    # Replay of the same signed epoch is rejected.
    ok, why = route_reward_broadcast(
        store=store, sender_hotkey=VALI1.ss58_address, epoch=EPOCH, seq=EPOCH + 50,
        uid_points={}, attestation=attestation_obj, attestation_sig=att_sig,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert (not ok) and why.startswith("duplicate_or_old_seq"), why

    # --- 4. Bad miner signature => no verdict (API-side rejection), lease still completes ---
    await _seed_tweets([2001])
    r = await client.get("/tweets/unscored", params={"limit": 1}, headers=vheaders(VALI2))
    bad_id = r.json()["tweets"][0]["id"]
    payload = sign_completed(MINER1, bad_id, Analysis("bull", "BTC", "news", "x", "y", "z"))
    payload["miner_signature"] = "00" * 64
    r = await client.post("/tweets/completed",
                          json={"completed_tweets": [payload]}, headers=vheaders(VALI2))
    assert r.status_code == 200, r.text
    assert len(await main.prisma.scoreverdict.find_many(
        where={"validatorHotkey": VALI2.ss58_address, "resourceId": str(bad_id)})) == 0
    assert len(await main.prisma.scoring.find_many(
        where={"tweetId": bad_id, "status": "completed"})) == 1

    # --- 5. Deep-verify recompute + audit divergence ---
    r = await client.get("/verdicts",
                         params={"validator": VALI1.ss58_address, "epoch": EPOCH},
                         headers=vheaders(VALI2))
    leaves = r.json()["verdicts"]
    expected_root = store.get_merkle_root(epoch=EPOCH, sender=VALI1.ss58_address)
    assert deep_verify.merkle_mismatch(expected_root, leaves) is None
    bad_leaves = [dict(x) for x in leaves]
    bad_leaves[0]["categorical_key"] = "TAMPERED"
    assert deep_verify.merkle_mismatch(expected_root, bad_leaves) is not None

    # Three validators score the same item+epoch: 2 agree, 1 diverges -> the minority is flagged.
    await _seed_tweets([3001])
    r = await client.get("/tweets/unscored", params={"limit": 1}, headers=vheaders(VALI1))
    shared_id = r.json()["tweets"][0]["id"]
    await client.post("/tweets/completed", headers=vheaders(VALI1), json={"completed_tweets": [
        sign_completed(MINER1, shared_id, Analysis("bull", "BTC", "news", "a", "b", "c"))]})
    for vk, sent in ((VALI2, "bull"), (VALI3, "bear")):
        await main.prisma.scoring.create(data={
            "tweetId": shared_id, "status": "in_progress", "validatorHotkey": vk.ss58_address})
        await client.post("/tweets/completed", headers=vheaders(vk), json={"completed_tweets": [
            sign_completed(MINER1, shared_id, Analysis(sent, "BTC", "news", "a", "b", "c"))]})
    divergent = await main.audit_divergence_for_group(f"tweet:{shared_id}:{EPOCH}")
    assert divergent == [VALI3.ss58_address], divergent

    # --- 6. Report consensus: alarm-only by default (§3), auto-blacklist only when enabled ---
    accused = VALI3.ss58_address
    main.REPORTS_AUTO_BLACKLIST = False  # explicit: default prod-safe posture
    r1 = await client.post("/reports", headers=vheaders(VALI1), json={
        "accused_hotkey": accused, "epoch": EPOCH, "reason": "content_divergence",
        "evidence": {"expected_root": "deadbeef"}})
    assert r1.json()["count"] == 1
    r2 = await client.post("/reports", headers=vheaders(VALI2), json={
        "accused_hotkey": accused, "epoch": EPOCH, "reason": "content_divergence",
        "evidence": {"expected_root": "deadbeef"}})
    assert r2.json()["count"] == 2  # consensus reached...
    bl = await client.get("/blacklist", headers=vheaders(VALI1))
    assert accused not in [b["hotkey"] for b in bl.json()], "alarm-only must NOT blacklist"

    # Flip the operator switch ON: the same consensus now auto-blacklists.
    main.REPORTS_AUTO_BLACKLIST = True
    try:
        r3 = await client.post("/reports", headers=vheaders(VALI2), json={
            "accused_hotkey": accused, "epoch": EPOCH, "reason": "content_divergence",
            "evidence": {"expected_root": "deadbeef"}})
        assert r3.json()["count"] == 2
        bl = await client.get("/blacklist", headers=vheaders(VALI1))
        assert accused in [b["hotkey"] for b in bl.json()], bl.json()
    finally:
        main.REPORTS_AUTO_BLACKLIST = False


def test_offline_ingest_adversarial():
    """Offline attestation ingest rejects forgery/tamper/replay/non-finite/over-cap/
    blacklist, and honours the Phase-3 enforce_signed switch. No DB required."""
    A = VALI1.ss58_address
    hotkey_to_uid = {MINER1.ss58_address: 10, MINER2.ss58_address: 11}
    per_miner = {MINER1.ss58_address: 2.0, MINER2.ss58_address: 1.0}
    root = "ab" * 32
    msg = val_ac.attestation_message(A, EPOCH, per_miner, 3.0, root)
    sig = ATT_KP.sign(msg.encode("utf-8")).hex()
    att = {"validatorHotkey": A, "epoch": EPOCH, "perMinerPoints": per_miner,
           "totalPoints": 3.0, "merkleRoot": root}

    def fresh():
        s = RewardBroadcastStore(path="/tmp/e2e_pytest_adv.json")
        s.last_seen_seq.clear(); s.by_epoch_by_sender.clear(); s.merkle_by_epoch_by_sender.clear()
        return s

    # Happy path establishes the baseline.
    s = fresh()
    ok, why = route_reward_broadcast(store=s, sender_hotkey=A, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation=att, attestation_sig=sig,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert ok, why

    # Forged signature.
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=A, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation=att, attestation_sig="00" * (len(sig)//2),
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert (not ok) and why == "bad_signature", why

    # Tampered payload, stale sig.
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=A, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation={**att, "perMinerPoints": {MINER1.ss58_address: 999.0},
                                    "totalPoints": 999.0}, attestation_sig=sig,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert (not ok) and why == "bad_signature", why

    # Sender mismatch.
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=VALI2.ss58_address,
        epoch=EPOCH, seq=EPOCH, uid_points={}, attestation=att, attestation_sig=sig,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert (not ok) and why == "sender_mismatch", why

    # Non-finite points dropped (re-signed so signature is valid).
    nf_pm = {MINER1.ss58_address: float("inf")}
    nf_msg = val_ac.attestation_message(A, EPOCH, nf_pm, 0.0, root)
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=A, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation={"validatorHotkey": A, "epoch": EPOCH, "perMinerPoints": nf_pm,
                                    "totalPoints": 0.0, "merkleRoot": root},
        attestation_sig=ATT_KP.sign(nf_msg.encode("utf-8")).hex(),
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert (not ok) and why == "empty_payload", why

    # Over-cap points dropped.
    big_pm = {MINER1.ss58_address: float(MAX_POINTS_PER_UID + 100)}
    big_msg = val_ac.attestation_message(A, EPOCH, big_pm, 0.0, root)
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=A, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation={"validatorHotkey": A, "epoch": EPOCH, "perMinerPoints": big_pm,
                                    "totalPoints": 0.0, "merkleRoot": root},
        attestation_sig=ATT_KP.sign(big_msg.encode("utf-8")).hex(),
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY)
    assert (not ok) and why == "empty_payload", why

    # Blacklisted sender refused first.
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=A, epoch=EPOCH, seq=EPOCH,
        uid_points={}, attestation=att, attestation_sig=sig,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY, blacklisted={A})
    assert (not ok) and why == "sender_blacklisted", why

    # Phase-3 enforcement drops unsigned/legacy.
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=VALI2.ss58_address,
        epoch=EPOCH, seq=EPOCH, uid_points={10: 5}, attestation=None, attestation_sig=None,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY, enforce_signed=True)
    assert (not ok) and why == "unsigned_broadcast_rejected", why

    # Legacy fallback accepted in grace window (legit broadcaster sets seq == epoch).
    ok, why = route_reward_broadcast(store=fresh(), sender_hotkey=VALI2.ss58_address,
        epoch=EPOCH, seq=EPOCH, uid_points={10: 5}, attestation=None, attestation_sig=None,
        hotkey_to_uid=hotkey_to_uid, pinned_pubkey=PINNED_PUBKEY, enforce_signed=False)
    assert ok, why


# ----------------------------------------------------------------------------
# News article leasing: /articles/unscored must serve BOTH rss and ccnews,
# newest-published first (branch B), with branch A (pending) served first.
# ----------------------------------------------------------------------------
async def _seed_article(url, title, source_type, published):
    """Create a news_articles row. published may be a datetime or None."""
    return await main.prisma.newsarticle.create(data={
        "url": url, "title": title, "source": "synthetic",
        "sourceType": source_type, "published": published,
    })


async def test_articles_unscored_serves_rss_and_ccnews(client):
    from datetime import datetime, timezone
    prev_gate = main.SERVE_NEWS_ARTICLES
    main.SERVE_NEWS_ARTICLES = True
    try:
        d_older = datetime(2026, 6, 10, tzinfo=timezone.utc)
        d_newer = datetime(2026, 6, 12, tzinfo=timezone.utc)
        d_rss   = datetime(2026, 6, 11, tzinfo=timezone.utc)

        # CC-NEWS rows have NO scoring record (mirrors ccnews_ingest bulk insert).
        cc_older = await _seed_article("http://x/cc-older", "CC older", "ccnews", d_older)
        cc_newer = await _seed_article("http://x/cc-newer", "CC newer", "ccnews", d_newer)
        cc_null  = await _seed_article("http://x/cc-null",  "CC null",  "ccnews", None)
        # RSS row WITH a pending scoring record (mirrors run.py _write_to_db).
        rss = await _seed_article("http://x/rss", "RSS one", "rss", d_rss)
        await main.prisma.newsarticlescoring.create(
            data={"articleId": rss.id, "status": "pending"})

        r = await client.get("/articles/unscored", params={"limit": 10},
                             headers=vheaders(VALI1))
        assert r.status_code == 200, r.text
        returned = [a["id"] for a in r.json()["articles"]]

        # Core fix: CC-NEWS articles are now served (previously rss-only).
        assert set(returned) == {rss.id, cc_newer.id, cc_older.id, cc_null.id}, returned

        # Approach 1 semantics: branch A (pending rss) first, then branch B
        # (ccnews) ordered newest-published first, NULL published last.
        assert returned == [rss.id, cc_newer.id, cc_older.id, cc_null.id], returned

        # Every leased article is now in_progress for this validator. The 3 CC-NEWS
        # rows had their scoring records created on-the-fly by branch B.
        for aid in (rss.id, cc_newer.id, cc_older.id, cc_null.id):
            rows = await main.prisma.newsarticlescoring.find_many(where={"articleId": aid})
            assert len(rows) == 1, (aid, rows)
            assert rows[0].status == "in_progress"
            assert rows[0].validatorHotkey == VALI1.ss58_address
    finally:
        main.SERVE_NEWS_ARTICLES = prev_gate
