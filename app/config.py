"""Environment-driven settings shared by the routers."""

import hashlib
import logging
import os
import time

from alpharidge_ai.ledger.attestation import BLOCK_LENGTH
from alpharidge_ai.mechanism import quality as qm
from alpharidge_ai.schema import OPEN_TYPES, TYPE_SPEC, EventType, category_of
from alpharidge_ai.schema.types import pay_settings, pay_switch
from alpharidge_ai.verify.fetch import user_agent
from app import ROOT


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _csv(key: str) -> list[str]:
    return _split(os.getenv(key, ""))


BLOCKED_HOTKEYS: set[str] = set(_csv("BLOCKED_HOTKEYS"))

# Hotkeys allowed on operator routes. Empty means no one.
OPERATOR_HOTKEYS: set[str] = set(_csv("OPERATOR_HOTKEYS"))

# Validators whose re-verify disagreements hold a claim; others are recorded only.
REVERIFY_VALIDATORS: set[str] = set(_csv("REVERIFY_VALIDATORS"))

# Peers allowed on internal-only routes; localhost is always allowed.
INTERNAL_ALLOWED_IPS: set[str] = {"127.0.0.1", "::1", *_csv("INTERNAL_ALLOWED_IPS")}

MIN_VALIDATOR_VERSION = os.getenv("MIN_VALIDATOR_VERSION", "4.0.0")

# "writer" (the default) runs everything; a "reader" serves only the read routes below, runs
# no jobs and writes nothing, so several can serve status and dashboard reads beside one writer.
API_ROLE = os.getenv("API_ROLE", "writer").strip().lower()
# Database connections per process: EL_DB_CONNECTION_LIMIT (EL_DB_READER_CONNECTION_LIMIT for
# a reader), and seconds a query waits for a free one; unset keeps Prisma's defaults.
DB_CONNECTION_LIMIT = os.getenv(
    "EL_DB_READER_CONNECTION_LIMIT" if API_ROLE == "reader" else "EL_DB_CONNECTION_LIMIT", "")
DB_POOL_TIMEOUT_S = os.getenv("EL_DB_POOL_TIMEOUT_S", "")
READER_ROUTES = (r"/health", r"/claims", r"/claims/\d+", r"/events(/.*)?", r"/entities/.+", r"/feed",
                 r"/dashboard/.+")

# Validator settings served on /config/subnet from VALIDATOR_<KEY> env vars, when set.
# The judge endpoint and model go to REVERIFY_VALIDATORS only.
_SERVED_KEYS = ("REVERIFY_FETCH_TIMEOUT_S", "REVERIFY_DOMAIN_RPS", "SCAN_INTERVAL_S",
                "JUDGE_POLL_S")
_JUDGE_KEYS = ("EL_JUDGE_URL", "EL_JUDGE_MODEL")


def validator_config(hotkey: str) -> dict:
    out = {k: os.environ[f"VALIDATOR_{k}"] for k in _SERVED_KEYS
           if os.getenv(f"VALIDATOR_{k}", "").strip()}
    contact = os.getenv("EL_USER_AGENT_CONTACT", "").strip()
    if contact:
        out["USER_AGENT_CONTACT"] = contact
    if hotkey in REVERIFY_VALIDATORS:
        out.update({k: os.environ[f"VALIDATOR_{k}"] for k in _JUDGE_KEYS
                    if os.getenv(f"VALIDATOR_{k}", "").strip()})
    return out

# Pay settings (EL_EVENT_PAY_X, EL_SCOOP_PREMIUM, EL_SCOOP_MISS_DEBT_X; the code's table by
# default), read from the environment when used. Every validator must use the same values: they
# are served on /config/subnet, and a validator whose own differ logs an alarm. A malformed value
# stops start-up here rather than at the first claim.
pay_settings()
# The pay table switch: EL_PAY_TABLE (a table in the code, "new") applies to claims received from
# EL_PAY_TABLE_FROM_MS on. Served on /config/subnet as {"pay_table", "from_ms"}; validators read
# it hourly, so set the start at least PAY_SWITCH_LEAD_MS ahead.
PAY_SWITCH_LEAD_MS = 2 * 3_600_000


def served_pay_switch():
    switch = pay_switch()
    return None if switch is None else {"pay_table": switch[0], "from_ms": switch[1]}


if pay_switch() is not None and pay_switch()[1] - time.time() * 1000 < PAY_SWITCH_LEAD_MS:
    logging.getLogger(__name__).warning(
        f"EL_PAY_TABLE_FROM_MS {pay_switch()[1]} is less than 2 h away: a validator that has "
        f"not read the switch by then prices claims after it at the old table")

# The quality start: EL_QUALITY_FROM_MS (ms; unset or 0 is off) turns the subnet's quality rule
# on for epochs signed from it. Served on /config/subnet as {"from_ms"}; validators read it
# hourly and apply it, so set it at least PAY_SWITCH_LEAD_MS ahead. The rule itself is the
# subnet code's; only its start is served.
def served_quality_switch():
    start = qm.start_ms()
    return None if start is None else {"from_ms": start}


if qm.start_ms() is not None and qm.start_ms() - time.time() * 1000 < PAY_SWITCH_LEAD_MS:
    logging.getLogger(__name__).warning(
        f"EL_QUALITY_FROM_MS {qm.start_ms()} is less than 2 h away: a validator that has not "
        f"read it by then scores epochs after it without quality")

VALIDATOR_VERSIONS_PATH = os.getenv(
    "VALIDATOR_VERSIONS_PATH", str(ROOT / ".validator_versions.json"))


def _el(key: str, default, cast=float):
    return cast(os.getenv(f"EL_{key}", str(default)))


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


# Event lane settings (EL_* env vars). Scoring, identity and trust values are constants
# next to the code that uses them.
EVENT_LANE = {
    # Event types accepted at intake; others are rejected as type_off.
    "TYPES": tuple(_csv("EL_TYPES") or (t.value for t in OPEN_TYPES)),
    # An untrusted page may be held as a scoop until a trusted page confirms it.
    "SCOOPS": _el("SCOOPS", "true", _flag),
    # Registry IDs (Wikidata, SEC, GLEIF): resolved once, vetted and cached in Entity.
    # "http" needs EL_USER_AGENT_CONTACT, else lookups stay off.
    "REGISTRY": _el("REGISTRY", "http", str),
    "API_UNVERIFIED": _el("API_UNVERIFIED", "true", _flag),
    "PRODUCT_KEYS_REQUIRED": _el("PRODUCT_KEYS_REQUIRED", "true", _flag),
    "RATE_LIMIT_ENABLED": _el("RATE_LIMIT_ENABLED", "true", _flag),
    # Crawler user agent: AlphaRidgeBot plus the operator's contact URL or email.
    "USER_AGENT": user_agent(_el("USER_AGENT_CONTACT", "", str)),
    # SEC asks for a user agent naming the operator and a contact address.
    "AK_SEC_USER_AGENT": _el("AK_SEC_USER_AGENT", "", str),
    # Page-judge items per hotkey per UTC day, and for hotkeys with no passes in 7 days.
    "JUDGE_HOTKEY_ITEMS": _el("JUDGE_HOTKEY_ITEMS", 2666, int),
    "JUDGE_NEW_HOTKEY_ITEMS": _el("JUDGE_NEW_HOTKEY_ITEMS", 266, int),
    # Shared with every validator, so not a setting.
    "BLOCK_LENGTH": BLOCK_LENGTH,
    "FETCH_TIMEOUT_S": _el("FETCH_TIMEOUT_S", 15),
    "FETCH_PUBLIC_ONLY": _el("FETCH_PUBLIC_ONLY", "true", _flag),
    "FETCH_RETRY": _el("FETCH_RETRY", 1, int),
    "DOMAIN_RPS": _el("DOMAIN_RPS", 1),
    # Requests per second for particular hosts, "host=rate,host=rate"; others use DOMAIN_RPS.
    "HOST_RPS": tuple((h.strip(), float(r)) for h, _, r in (
        part.partition("=") for part in _csv("EL_HOST_RPS")) if h.strip() and r.strip()),
    # Fetch and re-check jobs run outside the worker tick, in a bounded pool.
    "FETCH_CONCURRENCY": _el("FETCH_CONCURRENCY", 16, int),
    "WORKER_INTERVAL_S": _el("WORKER_INTERVAL_S", 1),
    "SETTLE_EVERY_S": _el("SETTLE_EVERY_S", 60),
    "SETTLE_DELAY_H": _el("SETTLE_DELAY_H", 72),
    "CHECK_BUDGET_BASE": _el("CHECK_BUDGET_BASE", 60, int),
    # Scoops: the event day and the page may end at most SCOOP_FRESH_H before receipt (0: no
    # limit). A coldkey holds at most SCOOP_OPEN_BASE + SCOOP_OPEN_PER_HIT per scoop confirmed
    # in the last 30 days open scoops at once, up to SCOOP_OPEN_MAX, and with 10 closed in that
    # time under SCOOP_ALLOW_LOW_RATE confirmed, none.
    "SCOOP_FRESH_H": _el("SCOOP_FRESH_H", 48),
    "SCOOP_OPEN_BASE": _el("SCOOP_OPEN_BASE", 5, int),
    "SCOOP_OPEN_PER_HIT": _el("SCOOP_OPEN_PER_HIT", 2, int),
    "SCOOP_OPEN_MAX": _el("SCOOP_OPEN_MAX", 50, int),
    "SCOOP_ALLOW_LOW_RATE": _el("SCOOP_ALLOW_LOW_RATE", 0.10),
    # Claims taken in at once; past it POST /claims answers 503 busy at once.
    "CLAIMS_IN_FLIGHT": _el("CLAIMS_IN_FLIGHT", 16, int),
    # Seconds a claim's status reply is reused before it is read again.
    "STATUS_CACHE_S": _el("STATUS_CACHE_S", 20),
    # Processes that parse fetched pages; 0 parses them on one thread.
    "PARSE_PROCESSES": _el("PARSE_PROCESSES", 0, int),
    # Processes for the fixed check and contrastive match; 0 runs them in the API process.
    "CHECK_PROCESSES": _el("CHECK_PROCESSES", 0, int),
    # Claims checked at once in a worker tick, one per hotkey at a time; 1 checks them in turn.
    "VERIFY_CONCURRENCY": _el("VERIFY_CONCURRENCY", 1, int),
    # Listed validators whose verdicts decide a page (0: all of them). With fewer than all,
    # each page goes to one of them, and to the others once it has waited JUDGE_HANDOFF_S.
    "JUDGES_PER_ITEM": _el("JUDGES_PER_ITEM", 0, int),
    # Operator diagnostics, off by default: log worker ticks slower than this many seconds,
    # and profile the process for this many seconds after start-up.
    "TICK_LOG_S": _el("TICK_LOG_S", 0),
    "PROFILE_S": _el("PROFILE_S", 0),
    "PUBLISH_HOLD_S": _el("PUBLISH_HOLD_S", 120),
    # Claims decided from these times (ms; 0 is off; TYPE_CHECK has been on since 16 Nov 2026
    # 10:56:37 UTC): a passed claim the judge's type answer
    # finds none of the types for is unclear (TYPE_CHECK); a passed claim the judge gives
    # another type is judged again as it (RETYPE_ON_PASS); names meet in other forms, and a
    # match result needs both sides and meets by both on the day (NAME_FORMS).
    "TYPE_CHECK_FROM_MS": _el("TYPE_CHECK_FROM_MS", 1_794_826_597_000, int),
    "RETYPE_ON_PASS_FROM_MS": _el("RETYPE_ON_PASS_FROM_MS", 0, int),
    "NAME_FORMS_FROM_MS": _el("NAME_FORMS_FROM_MS", 0, int),
    # The least judge confidence in "none of the types" that makes a passed claim unclear.
    "TYPE_NONE_MIN": _el("TYPE_NONE_MIN", 0.5),
    # Paid claims from one host and type in an hour and in a day past which the operator is
    # alerted; "host=hour/day,..." sets other limits for hosts that publish many events.
    "ALARM_SOURCE_HOUR": _el("ALARM_SOURCE_HOUR", 20, int),
    "ALARM_SOURCE_DAY": _el("ALARM_SOURCE_DAY", 50, int),
    "ALARM_SOURCE_HOSTS": os.getenv("EL_ALARM_SOURCE_HOSTS", ""),
    # Coldkey quality (alpharidge_ai.mechanism.quality) for the budget and the dashboard,
    # scored from claims decided (paid or rejected) since BUDGET_FROM_MS, in the order they
    # were decided; at 0 (the default), from the last 30 days (app.services.quality.start).
    # Separate from the quality start (EL_QUALITY_FROM_MS), so turning the pay rule on does not
    # restart anyone's budget score. Validators ask from the quality start.
    # QUALITY_BUDGET scales each hotkey's check budget by its coldkey's multiplier, down to
    # QUALITY_BUDGET_MIN.
    "BUDGET_FROM_MS": _el("BUDGET_FROM_MS", 0, int),
    "QUALITY_BUDGET": _flag(os.getenv("EL_QUALITY_BUDGET", "")),
    "QUALITY_BUDGET_MIN": _el("QUALITY_BUDGET_MIN", 20, int),
    # The worker deletes lines left with rejected claims only (app.services.line.drop_dead).
    "DROP_DEAD_LINES": _flag(os.getenv("EL_DROP_DEAD_LINES", "")),
    # Answer key: official feeds, private, used for accuracy only.
    # Set but empty means none.
    "AK_ADAPTERS": tuple(_csv("EL_AK_ADAPTERS") if "EL_AK_ADAPTERS" in os.environ
                         else ("edgar", "fed", "bls", "bea")),
    "AK_BLS_URL": _el("AK_BLS_URL", "https://www.bls.gov/feed/bls_latest.rss", str),
    "AK_BLS_CALENDAR_URL": _el("AK_BLS_CALENDAR_URL",
                               "https://www.bls.gov/schedule/news_release/bls.ics", str),
    # Later re-check of paid sources.
    "RECHECK_PROXY": _el("RECHECK_PROXY", "", str),
    # Set only when the proxy itself refuses non-public addresses.
    "RECHECK_PROXY_PUBLIC_ONLY": _el("RECHECK_PROXY_PUBLIC_ONLY", "false", _flag),
}

_untiered = [t for t in EVENT_LANE["TYPES"] if not TYPE_SPEC[EventType(t)].tier]
if _untiered:
    raise ValueError(f"EL_TYPES without a price tier in types.json: {_untiered}")

# Categories of the accepted types.
CATEGORIES = tuple(sorted({category_of(t).value for t in EVENT_LANE["TYPES"]}))

# Secrets read from the environment only, never listed in EVENT_LANE.
RECHECK_KEY = os.getenv("EL_RECHECK_KEY", "").encode() or os.urandom(32)


def _private(key: str, cast=float):
    raw = os.getenv(f"EL_{key}", "").strip()
    return cast(raw) if raw else None


def _tuple(value: str) -> tuple:
    return tuple(_split(value))


# Settings with no public default: the API refuses to start until each is set.
PRIVATE = {
    "RECHECK_SAMPLE_RATE": _private("RECHECK_SAMPLE_RATE"),
    "RECHECK_LOW_TRUST": _private("RECHECK_LOW_TRUST"),
    "RECHECK_MIN_H": _private("RECHECK_MIN_H"),
    "RECHECK_MAX_H": _private("RECHECK_MAX_H"),
    "RECHECK_ACCEPT_LANGUAGE": _private("RECHECK_ACCEPT_LANGUAGE", str),
    "RECHECK_NOTICES": _private("RECHECK_NOTICES", _tuple),
    "PAGE_FLAG_DAYS": _private("PAGE_FLAG_DAYS"),
    "PAGE_FLAG_BYLINES_MIN": _private("PAGE_FLAG_BYLINES_MIN", int),
    "PAGE_FLAG_BYLINES_SHARE": _private("PAGE_FLAG_BYLINES_SHARE"),
    "PAGE_FLAG_CONTRIBUTOR_MIN": _private("PAGE_FLAG_CONTRIBUTOR_MIN", int),
    "PAGE_FLAG_UNDATED_MIN": _private("PAGE_FLAG_UNDATED_MIN", int),
    "PAGE_FLAG_UNDATED_SHARE": _private("PAGE_FLAG_UNDATED_SHARE"),
    "PAGE_FLAG_MARKERS": _private("PAGE_FLAG_MARKERS", _tuple),
    "ALARM_CLAW_DAYS": _private("ALARM_CLAW_DAYS"),
    "ALARM_CLAW_MIN": _private("ALARM_CLAW_MIN", int),
    "ALARM_CLAW_SHARE": _private("ALARM_CLAW_SHARE"),
    "ALARM_COLDKEY_HOTKEYS": _private("ALARM_COLDKEY_HOTKEYS", int),
    "ALARM_COLDKEY_MIN": _private("ALARM_COLDKEY_MIN", int),
    "ALARM_COLDKEY_SHARE": _private("ALARM_COLDKEY_SHARE"),
    "ALARM_FETCH_WINDOW_H": _private("ALARM_FETCH_WINDOW_H"),
    "ALARM_FETCH_MIN": _private("ALARM_FETCH_MIN", int),
    "ALARM_FETCH_FAIL_SHARE": _private("ALARM_FETCH_FAIL_SHARE"),
    "AUDIT_DAYS": _private("AUDIT_DAYS"),
    "AUDIT_N": _private("AUDIT_N", int),
    "AUDIT_MIN_N": _private("AUDIT_MIN_N", int),
    "AUDIT_ALARM_LB": _private("AUDIT_ALARM_LB"),
    "HEDGE_GAP_S": _private("HEDGE_GAP_S"),
    "TRUST_SPIKE_X": _private("TRUST_SPIKE_X"),
    "TRUST_SPIKE_MIN": _private("TRUST_SPIKE_MIN", int),
    "TRUST_SPIKE_BASE_DAYS": _private("TRUST_SPIKE_BASE_DAYS"),
}


def missing_private() -> list[str]:
    return [f"EL_{k}" for k, v in PRIVATE.items() if v is None]


# Product API keys from the environment, as sha256 hex; more live in the api_key table.
PRODUCT_API_KEYS: set[str] = {hashlib.sha256(k.encode()).hexdigest()
                              for k in _csv("PRODUCT_API_KEYS")}
