"""Environment-driven settings shared by the routers."""

import hashlib
import os

from alpharidge_ai.ledger.attestation import BLOCK_LENGTH
from alpharidge_ai.schema import OPEN_TYPES, TYPE_SPEC, EventType, category_of
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
READER_ROUTES = (r"/health", r"/claims/\d+", r"/events(/.*)?", r"/entities/.+", r"/feed",
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
    # Fetch and re-check jobs run outside the worker tick, in a bounded pool.
    "FETCH_CONCURRENCY": _el("FETCH_CONCURRENCY", 16, int),
    "WORKER_INTERVAL_S": _el("WORKER_INTERVAL_S", 1),
    "SETTLE_EVERY_S": _el("SETTLE_EVERY_S", 60),
    "SETTLE_DELAY_H": _el("SETTLE_DELAY_H", 72),
    "CHECK_BUDGET_BASE": _el("CHECK_BUDGET_BASE", 200, int),
    # Claims taken in at once; past it POST /claims answers 503 busy at once.
    "CLAIMS_IN_FLIGHT": _el("CLAIMS_IN_FLIGHT", 16, int),
    # Seconds a claim's status reply is reused before it is read again.
    "STATUS_CACHE_S": _el("STATUS_CACHE_S", 20),
    # Processes that parse fetched pages; 0 parses them on one thread.
    "PARSE_PROCESSES": _el("PARSE_PROCESSES", 0, int),
    # Operator diagnostics, off by default: log worker ticks slower than this many seconds,
    # and profile the process for this many seconds after start-up.
    "TICK_LOG_S": _el("TICK_LOG_S", 0),
    "PROFILE_S": _el("PROFILE_S", 0),
    "PUBLISH_HOLD_S": _el("PUBLISH_HOLD_S", 120),
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
