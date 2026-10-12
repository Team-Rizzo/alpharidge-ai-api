"""
Relevance gate for downstream consumers.

Decides, once per article, whether an analysed article may leave the API for
downstream use (feeds, aggregates, event clustering, narratives). Two parts:

1. Content rules (deterministic): tag/listing/index pages and pages with no
   article body are not eligible.
2. Relevance check (LLM): an eligible article a miner labelled relevant or
   borderline gets one judge call. The verdict is stored and never recomputed.

Verdicts live in the article_gate table so that every reader, inside this API
or not, can filter on the same row. This is independent of miner pay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("relevance_gate")

JUDGE_LABELS = ("relevant", "borderline")
JUDGE_BODY_CHARS = 1600
JUDGE_MAX_ATTEMPTS = 3
MIN_PROSE_CHARS = 80
POLL_SECONDS = float(os.getenv("GATE_POLL_SECONDS", "15"))
LOOKBACK_HOURS = int(os.getenv("GATE_LOOKBACK_HOURS", "3"))
BATCH_LIMIT = int(os.getenv("GATE_BATCH_LIMIT", "1000"))
JUDGE_CONCURRENCY = int(os.getenv("GATE_JUDGE_CONCURRENCY", "8"))
CONTENT_CHARS = 8000

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

JUDGE_PROMPT = """Would a professional who trades financial markets want this article in their news feed?

RELEVANT: news of a development that could plausibly move the price of listed shares, bonds, commodities, currencies or interest rates. For example: results, deals, products, legal or regulatory events of listed or major companies; economic data; central bank or government economic and fiscal policy; regulation affecting an industry; energy, commodity, food or shipping supply; sanctions, tariffs, export controls; geopolitical events with a clear economic channel; elections or government changes with clear policy stakes.

NOT RELEVANT: sport, entertainment, celebrity, lifestyle, religion, weather, local news, crime and accidents without wider economic effect, job adverts, product support pages, advertorials and paid "market size" reports or promotional press releases, old videos or opinion pieces with no news, political statements, campaigns or protests without a concrete economic consequence.

TITLE: {title}

ARTICLE (may be truncated):
{body}

Answer with only JSON: {"relevant": true or false}"""


# ============================================================================
# Content rules
# ============================================================================

_TAG_WORDS = (
    r"тег|теги|метка|метки|tag|tags|tagged|schlagwort|stichwort|etiqueta|etiquetas|"
    r"étiquette|etichetta|tagi|címke|štítok|štítek|标签|タグ|태그"
)
_LISTING_TITLE = (
    re.compile(rf"^\s*(?:{_TAG_WORDS})\s*[:：]\s*(?P<rest>.+)$", re.I),
    re.compile(
        r"(?:latest news (?:about|on)|(?:news|posts|articles|stories) tagged|"
        r"новости по тегу|новини за тегом|beiträge (?:zum|mit dem) (?:thema|schlagwort))"
        r"\s*[:：]?\s*(?P<rest>.+)$",
        re.I,
    ),
)
# Trailing " | Site" / " - Site" on a page title.
_SITE_SUFFIX = re.compile(r"\s+[|–—-]\s+[^|–—-]{1,40}$")
_LISTING_SUBJECT_MAX_WORDS = 6

_LISTING_BODY = re.compile(
    r"новости по тегу|новини за тегом|материалы .{0,20}по ключевому слову|"
    r"(?:posts|articles|news|stories) tagged|browsing tag|showing posts with label|"
    r"beiträge mit dem schlagwort|artículos con la etiqueta|articles avec le tag",
    re.I,
)
_LISTING_BODY_SCAN = 800

_SEG_SPLIT = re.compile(r"\n|\s/\s|\s\|\s|\s{3,}|\t")
_WIDE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_WORD = re.compile(r"[^\W_]+")


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def _strip_title(title: str, body: str) -> str:
    if title and body.startswith(title):
        return body[len(title):]
    return body


def _listing_title(title: str) -> bool:
    for pattern in _LISTING_TITLE:
        m = pattern.search(title)
        if not m:
            continue
        subject = _SITE_SUFFIX.sub("", m.group("rest").strip())
        if subject and len(_WORD.findall(subject)) <= _LISTING_SUBJECT_MAX_WORDS \
                and not re.search(r"[.?!]$", subject):
            return True
    return False


def prose_chars(title: str, body: str) -> int:
    """Characters of running text in the body, ignoring navigation, repeats and the
    title itself. Wide (CJK) characters count three times."""
    body = _strip_title(title, body)
    title_n = _norm(title)
    seen = set()
    total = 0
    for seg in _SEG_SPLIT.split(body):
        seg = seg.strip(" .:-")
        n = _norm(seg)
        if not n or n in seen:
            continue
        seen.add(n)
        if title_n and (n in title_n or (title_n in n and len(n) < len(title_n) + 25)):
            continue
        wide = len(_WIDE.findall(seg))
        if wide >= 20:
            total += wide * 3 + (len(seg) - wide)
            continue
        words = _WORD.findall(seg)
        # Eight words, or long unspaced runs (scripts written without spaces).
        if len(seg) >= 50 and (len(words) >= 8 or sum(map(len, words)) >= 12 * len(words)):
            total += len(seg)
    return total


def content_reason(title: Optional[str], body: Optional[str],
                   min_prose: int = MIN_PROSE_CHARS) -> Optional[str]:
    """Why the page is not an article, or None when it passes. min_prose 0 turns the
    no-body rule off."""
    title = (title or "").strip()
    body = body or ""
    if _listing_title(title):
        return "listing_title"
    rest = _strip_title(title, body)
    if _LISTING_BODY.search(rest[:_LISTING_BODY_SCAN]):
        return "listing_body"
    if prose_chars(title, body) < min_prose:
        return "no_body"
    return None


def classify(title: Optional[str], body: Optional[str], label: Optional[str],
             min_prose: int = MIN_PROSE_CHARS) -> tuple[bool, Optional[str]]:
    """(eligible, reason) before any judge call."""
    if label is not None and label not in JUDGE_LABELS:
        return False, "miner_label"
    reason = content_reason(title, body, min_prose)
    return reason is None, reason


# ============================================================================
# Served settings
# ============================================================================

def _config() -> dict:
    from main import SUBNET_CONFIG
    return SUBNET_CONFIG


def gate_enabled(cfg: Optional[dict] = None) -> bool:
    return bool((cfg or _config()).get("DOWNSTREAM_GATE_ENABLED", False))


def downstream_sql(article_col: str, cfg: Optional[dict] = None) -> str:
    """SQL condition (with a leading AND) keeping only articles downstream may see.
    Empty when the gate is off. `article_col` must be a trusted column reference."""
    cfg = cfg or _config()
    if not gate_enabled(cfg):
        return ""
    judge = bool(cfg.get("DOWNSTREAM_JUDGE_ENABLED", True))
    if cfg.get("DOWNSTREAM_GATE_FAIL_OPEN", False):
        bad = "NOT g.eligible OR g.relevant IS FALSE" if judge else "NOT g.eligible"
        return (f" AND NOT EXISTS (SELECT 1 FROM article_gate g "
                f"WHERE g.article_id = {article_col} AND ({bad}))")
    ok = "g.eligible AND g.relevant IS TRUE" if judge else "g.eligible"
    return (f" AND EXISTS (SELECT 1 FROM article_gate g "
            f"WHERE g.article_id = {article_col} AND {ok})")


def holds_clustering(cfg: Optional[dict] = None) -> bool:
    """When the gate is on, clustering and narrative matching run from the gate loop
    after an article passes, not at submission."""
    return gate_enabled(cfg)


# ============================================================================
# Relevance judge
# ============================================================================

def build_prompt(title: Optional[str], body: Optional[str]) -> str:
    return (JUDGE_PROMPT
            .replace("{title}", (title or "").strip())
            .replace("{body}", (body or "")[:JUDGE_BODY_CHARS]))


def parse_verdict(text: Optional[str]) -> Optional[bool]:
    if not text:
        return None
    m = re.search(r"\{.*?\}", text, re.S)
    if not m:
        return None
    try:
        value = json.loads(m.group(0)).get("relevant")
    except (ValueError, AttributeError):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


async def judge(http: httpx.AsyncClient, api_key: str, model: str, title: Optional[str],
                body: Optional[str]) -> Optional[bool]:
    """One judge call. True/False, or None when no usable answer came back."""
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 20,
        "messages": [{"role": "user", "content": build_prompt(title, body)}],
        "response_format": {"type": "json_object"},
        "reasoning": {"enabled": False},
    }
    resp = await http.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )
    resp.raise_for_status()
    choices = resp.json().get("choices") or []
    if not choices:
        return None
    return parse_verdict((choices[0].get("message") or {}).get("content"))


# ============================================================================
# Gate loop
# ============================================================================

class _Counters:
    def __init__(self):
        self.reset()
        self.day = None
        self.calls_today = 0
        self.cap_logged = False

    def reset(self):
        self.checked = self.passed = self.blocked = self.errors = self.judged = 0
        self.capped = 0
        self.since = time.monotonic()

    def maybe_log(self, interval: float = 3600.0):
        if time.monotonic() - self.since < interval:
            return
        logger.info(
            f"[GATE] last hour: checked={self.checked} passed={self.passed} "
            f"blocked={self.blocked} errors={self.errors} judged={self.judged} "
            f"capped={self.capped} judge_calls_today={self.calls_today}"
        )
        self.reset()


_CANDIDATES_SQL = """
    SELECT na.id AS article_id, na.title, LEFT(na.content, $5::int) AS content,
           naa.analysis_data->'triage'->>'label' AS label,
           g.article_id IS NOT NULL AS has_gate, g.eligible, g.relevant,
           g.judge_attempts, g.miner_label AS gate_label
    FROM news_article_analysis naa
    JOIN news_articles na ON na.id = naa.article_id
    LEFT JOIN article_gate g ON g.article_id = naa.article_id
    WHERE naa.analyzed_at >= NOW() - make_interval(hours => $1::int)
      AND (g.article_id IS NULL
           OR g.miner_label IS DISTINCT FROM (naa.analysis_data->'triage'->>'label')
           OR (g.eligible AND g.relevant IS NULL AND $6::boolean AND g.judge_attempts < $2::int)
           OR (g.eligible AND g.clustered_at IS NULL AND (g.relevant IS TRUE OR NOT $3::boolean)))
    ORDER BY naa.analyzed_at DESC
    LIMIT $4
"""


async def _calls_today(prisma) -> int:
    rows = await prisma.query_raw(
        "SELECT COUNT(*)::int AS n FROM article_gate "
        "WHERE judged_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    )
    return int(rows[0]["n"]) if rows else 0


async def _cluster(prisma, article_id: int) -> None:
    rows = await prisma.query_raw(
        "SELECT analysis_data FROM news_article_analysis WHERE article_id = $1", article_id,
    )
    ad = rows[0].get("analysis_data") if rows else None
    if isinstance(ad, str):
        try:
            ad = json.loads(ad)
        except ValueError:
            ad = None
    if isinstance(ad, dict):
        try:
            from services.event_clustering import cluster_article
            from services.narrative_matcher import match_article_narratives
            await cluster_article(prisma, article_id, ad)
            emb = ad.get("narrative_embedding")
            if not (isinstance(emb, list) and len(emb) == 384):
                emb = None
            await match_article_narratives(
                prisma, article_id, ad.get("narrative_keywords", []),
                (ad.get("topic_signature") or {}).get("primary_sector_id"),
                narrative_embedding=emb,
            )
        except Exception as e:
            logger.warning(f"[GATE] clustering failed for article {article_id}: {e!r}")
    await prisma.execute_raw(
        "UPDATE article_gate SET clustered_at = NOW() WHERE article_id = $1", article_id,
    )


async def gate_pass(prisma, http: httpx.AsyncClient, counters: _Counters, cfg: dict,
                    api_key: str) -> int:
    """One pass over recent analyses. Returns the number of rows looked at."""
    judge_on = bool(cfg.get("DOWNSTREAM_JUDGE_ENABLED", True))
    min_prose = int(cfg.get("DOWNSTREAM_GATE_MIN_PROSE_CHARS", MIN_PROSE_CHARS))
    room = 0
    if judge_on:
        today = datetime.now(timezone.utc).date()
        if counters.day != today:
            counters.day = today
            counters.calls_today = await _calls_today(prisma)
            counters.cap_logged = False
        room = max(0, int(cfg.get("DOWNSTREAM_JUDGE_DAILY_CAP", 0) or 0) - counters.calls_today)
        if room == 0 and not counters.cap_logged:
            logger.warning("[GATE] daily judge cap reached: relevant articles wait until tomorrow")
            counters.cap_logged = True
    rows = await prisma.query_raw(
        _CANDIDATES_SQL, LOOKBACK_HOURS, JUDGE_MAX_ATTEMPTS, judge_on, BATCH_LIMIT,
        CONTENT_CHARS, room > 0,
    )
    to_judge = []
    to_cluster = []
    for r in rows:
        aid = int(r["article_id"])
        label = r.get("label")
        if not r.get("has_gate") or r.get("gate_label") != label:
            eligible, reason = classify(r.get("title"), r.get("content"), label, min_prose)
            await prisma.execute_raw(
                """
                INSERT INTO article_gate (article_id, eligible, reason, miner_label)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (article_id) DO UPDATE
                SET eligible = EXCLUDED.eligible, reason = EXCLUDED.reason,
                    miner_label = EXCLUDED.miner_label, updated_at = NOW()
                """,
                aid, eligible, reason, label,
            )
            counters.checked += 1
            if not eligible:
                counters.blocked += 1
                continue
            relevant = r.get("relevant") if r.get("has_gate") else None
        else:
            relevant = r.get("relevant")
        if not judge_on:
            counters.passed += 1
            to_cluster.append(aid)
        elif relevant is None:
            to_judge.append(r)
        elif relevant:
            to_cluster.append(aid)

    if to_judge:
        if len(to_judge) > room:
            counters.capped += len(to_judge) - room
            to_judge = to_judge[:room]
        model = str(cfg.get("DOWNSTREAM_JUDGE_MODEL") or "deepseek/deepseek-v4-flash")
        sem = asyncio.Semaphore(max(1, JUDGE_CONCURRENCY))

        async def _one(r):
            aid = int(r["article_id"])
            async with sem:
                counters.calls_today += 1
                try:
                    verdict = await judge(http, api_key, model, r.get("title"), r.get("content"))
                except Exception as e:
                    logger.debug(f"[GATE] judge call failed for {aid}: {e!r}")
                    verdict = None
            if verdict is None:
                counters.errors += 1
                await prisma.execute_raw(
                    "UPDATE article_gate SET judge_attempts = judge_attempts + 1, updated_at = NOW() "
                    "WHERE article_id = $1", aid,
                )
                return
            counters.judged += 1
            await prisma.execute_raw(
                "UPDATE article_gate SET relevant = $2, judge_model = $3, judged_at = NOW(), "
                "judge_attempts = judge_attempts + 1, updated_at = NOW() WHERE article_id = $1",
                aid, verdict, model,
            )
            if verdict:
                counters.passed += 1
                to_cluster.append(aid)
            else:
                counters.blocked += 1

        await asyncio.gather(*(_one(r) for r in to_judge))

    for aid in to_cluster:
        await _cluster(prisma, aid)
    return len(rows)


async def run_gate_loop(prisma, cfg: dict) -> None:
    """Background loop. Started once at API startup when the gate is enabled."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if cfg.get("DOWNSTREAM_JUDGE_ENABLED", True) and not api_key:
        logger.warning("[GATE] OPENROUTER_API_KEY is not set: judge calls will fail and "
                       "relevant articles stay unchecked")
    counters = _Counters()
    async with httpx.AsyncClient(timeout=30.0) as http:
        while True:
            try:
                n = await gate_pass(prisma, http, counters, cfg, api_key)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GATE] pass failed: {e!r}")
                n = 0
            counters.maybe_log()
            await asyncio.sleep(POLL_SECONDS if n < BATCH_LIMIT else 1.0)
