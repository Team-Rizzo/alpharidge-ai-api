"""Store rows and the Store protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from app.services.entities import EntityRecord


# Claims that use no check budget: rejected before any check, or left unjudged.
INTAKE_REJECTS = ("kind_off", "type_off", "role_off", "judge_unavailable")
# Routes that take an event prize from their source document.
PRIZE_ROUTES = ("event", "scoop")
# Claims that hold what they won.
WON = ("verified", "provisional", "settled")
# Claims that count as passes for the check budget.
PASSED = ("provisional", "settled")
# Scoop outcomes that count against a hotkey's scoop record.
SCOOP_MISSES = ("scoop_unconfirmed", "scoop_mismatch")


class DuplicateClaim(Exception):
    """The (hotkey, nonce) or (hotkey, body hash) pair already exists."""


class DuplicateEvent(Exception):
    """An event with this fingerprint or identity key already exists."""


@dataclass
class ClaimRow:
    hotkey: str
    signature: str
    nonce: str
    signed_at: Optional[int]
    body_hash: str
    body: str
    received_ms: int
    bucket: int
    kind: str
    category: str
    event_type: Optional[str]
    payload: dict
    source_url: str
    status: str = "received"
    reason: Optional[str] = None
    event_id: Optional[int] = None
    novelty: Optional[str] = None
    route: Optional[str] = None
    held: bool = False
    verified_ms: Optional[int] = None
    judged_ms: Optional[int] = None
    settle_ms: Optional[int] = None
    pending_of_event_id: Optional[int] = None
    doc_hash: Optional[str] = None
    source_id: Optional[int] = None
    # Source trust and band frozen at verification.
    trust: Optional[float] = None
    trust_band: Optional[str] = None
    wrong: Optional[str] = None
    # The hotkey's coldkey at receipt.
    coldkey: Optional[str] = None
    id: int = 0


@dataclass
class EventRecord:
    """An event as the novelty index sees it. confidence "line" is an open line."""
    event_type: str
    category: str
    event_date: date
    fingerprint: str
    headline: str = ""
    actor_keys: Tuple[str, ...] = ()
    # Names and aliases of every actor, for the post-fetch checks.
    names: Tuple[str, ...] = ()
    subject_keys: Tuple[str, ...] = ()
    # Figures of the claim that opened the event, as (field, value, unit).
    numbers: Tuple[Tuple[str, float, Optional[str]], ...] = ()
    place_key: Optional[str] = None
    identity_key: Optional[str] = None
    stage: Optional[str] = None
    first_claim_id: Optional[int] = None
    confidence: str = "reported"
    first_seen_ms: Optional[int] = None
    official_match: bool = False
    publish_ms: Optional[int] = None
    hold: Optional[str] = None
    storyline_id: Optional[int] = None
    marked: Optional[str] = None
    id: int = 0


@dataclass
class SourceFetchRow:
    """One fetch of a claim's source. Hashes, offsets and metadata only, never the text."""
    claim_id: int
    verdict: str
    http_status: Optional[int] = None
    content_hash: Optional[str] = None
    spans: Optional[list] = None
    final_url: Optional[str] = None
    canonical_url: Optional[str] = None
    published_ms: Optional[int] = None
    bytes: Optional[int] = None
    page_type: Optional[str] = None
    reason: Optional[str] = None
    check: Optional[dict] = None
    fetched_ms: Optional[int] = None
    id: int = 0


@dataclass
class ClaimHead:
    """The few columns of a claim a scan orders by, without its body."""
    id: int
    event_id: Optional[int]
    source_url: str
    received_ms: int
    event_date: Optional[str]


@dataclass
class Job:
    id: int
    kind: str
    claim_id: Optional[int]
    attempts: int = 0
    status: str = "pending"
    error: Optional[str] = None
    run_ms: int = 0


@dataclass
class ScoopCandidateRow:
    """A page a validator found for an open scoop."""
    scoop_id: int
    url: str
    validator: str
    found_ms: int
    status: str = "queued"
    reason: Optional[str] = None
    id: int = 0


@dataclass
class JudgeItemRow:
    """A claim or a found page waiting for the listed validators' page verdicts."""
    key: str
    claim_id: int
    kind: str
    task: dict
    created_ms: int
    hotkey: Optional[str] = None
    status: str = "pending"
    id: int = 0


@dataclass
class JudgePostRow:
    """One validator's posted verdict on a judge item, with its signature over the body."""
    item_id: int
    validator: str
    body: str
    signed_at: int
    nonce: str
    signature: str
    posted_ms: int
    id: int = 0


@dataclass
class HotkeyStats:
    passes: int = 0


@dataclass
class LedgerRow:
    hotkey: str
    claim_id: Optional[int]
    event_id: Optional[int]
    kind: str
    points: float
    epoch: int
    route: Optional[str] = None
    category: Optional[str] = None
    factors: Optional[dict] = None
    reason: Optional[str] = None
    received_ms: Optional[int] = None
    bucket: Optional[int] = None
    coldkey: Optional[str] = None
    id: int = 0


@dataclass
class EventFactRow:
    """One fact on an event. Status is accepted, pending or offschema; only accepted fills a slot."""
    event_id: int
    field: str
    value: object
    unit: Optional[str] = None
    claim_id: Optional[int] = None
    slot: Optional[str] = None
    doc_hash: Optional[str] = None
    status: str = "accepted"
    paid: bool = False
    source_url: Optional[str] = None
    id: int = 0


@dataclass
class ReverifyRow:
    claim_id: int
    validator: str
    epoch: int
    verdict: str
    observed: dict = field(default_factory=dict)
    id: int = 0


@dataclass
class SourceRow:
    """A host ("*.host" for it and its subdomains) plus path prefix ("" for the whole host),
    filed under its registrable domain."""
    domain: str
    path_rule: str = ""
    owner_id: Optional[str] = None
    status: str = "pending"
    base_trust: float = 0.1
    category_trust: dict = field(default_factory=dict)
    reset_ms: Optional[int] = None
    id: int = 0
    host: str = ""

    def __post_init__(self):
        if not self.host:
            self.host = self.domain


@dataclass
class OwnerRow:
    id: str
    evidence: dict = field(default_factory=dict)
    first_seen_ms: int = 0


@dataclass
class SourceTrustRow:
    source_id: int
    category: str
    base: float
    earned: float = 0.0
    penalties: float = 0.0
    trust: float = 0.1
    judged_firsts: float = 0.0
    confirmed_firsts: float = 0.0
    confirm_owners: int = 0


@dataclass
class SourceEventRow:
    source_id: int
    role: str
    category: str
    at_ms: int
    event_id: Optional[int] = None
    claim_id: Optional[int] = None
    owner_id: Optional[str] = None
    by_source_id: Optional[int] = None
    published_ms: Optional[int] = None
    id: int = 0


@dataclass
class PageRuleRow:
    pattern: str
    kind: str
    reason: str
    created_by: str = "seed"
    id: int = 0


@dataclass
class TrustLogRow:
    field: str
    old: Optional[str]
    new: Optional[str]
    reason: str
    actor: str
    source_id: Optional[int] = None
    rule_id: Optional[int] = None
    at_ms: int = 0
    id: int = 0


@dataclass
class AnswerKeyRow:
    """One record from an official feed. Kept private; never creates an event."""
    adapter: str
    external_id: str
    payload: dict
    category: Optional[str] = None
    event_type: Optional[str] = None
    event_date: Optional[date] = None
    published_ms: Optional[int] = None
    status: str = "open"
    matched_claim_id: Optional[int] = None
    matched_event_id: Optional[int] = None
    matched_ms: Optional[int] = None
    observed_ms: int = 0
    id: int = 0


@dataclass
class AccuracyCheckRow:
    claim_id: int
    hotkey: str
    category: str
    kind: str
    correct: bool
    at_ms: int
    ref: str = ""
    id: int = 0


@dataclass
class MinerAccuracyRow:
    hotkey: str
    category: str
    n: int
    correct: int
    q: float


@dataclass
class FeedRow:
    """One product feed record. The payload is public."""
    kind: str
    event_id: int
    category: str
    event_type: str
    at_ms: int
    payload: dict = field(default_factory=dict)
    seq: int = 0


@dataclass
class EventQuery:
    """Published product events, newest first; `before` is a (first_seen_ms, id) cursor."""
    published_by_ms: int
    confidences: Tuple[str, ...]
    category: Optional[str] = None
    event_type: Optional[str] = None
    stage: Optional[str] = None
    entity_keys: Tuple[str, ...] = ()
    since_ms: Optional[int] = None
    until_ms: Optional[int] = None
    before: Optional[Tuple[int, int]] = None
    storyline_id: Optional[int] = None
    limit: int = 50


@dataclass
class MinerStats:
    claims: int = 0
    verified: int = 0
    rejected: Dict[str, int] = field(default_factory=dict)
    # route -> ledger kind -> points
    points: Dict[str, Dict[str, float]] = field(default_factory=dict)


class Store(Protocol):
    async def is_blacklisted(self, hotkey: str) -> bool: ...
    async def set_blacklisted(self, hotkey: str, blocked: bool, reason: Optional[str] = None) -> None: ...
    async def claim_by_body(self, hotkey: str, body_hash: str) -> Optional[ClaimRow]: ...
    async def create_claim(self, row: ClaimRow, jobs: Sequence[str] = (),
                           receipt: Optional[Callable[[int, Optional[dict]], dict]] = None
                           ) -> ClaimRow: ...
    async def receipt_of(self, claim_id: int) -> Optional[dict]: ...
    async def receipts_for(self, claim_ids: Sequence[int]) -> Dict[int, dict]: ...
    async def get_claim(self, claim_id: int) -> Optional[ClaimRow]: ...
    async def update_claim(self, claim_id: int, expect: str, **fields) -> bool: ...
    async def claims_by_status(self, status: str, received_before_ms: Optional[int] = None,
                               limit: int = 500, skip: Sequence[int] = ()) -> List[ClaimRow]: ...
    async def judge_repeats(self, owner: str, event_type: str, doc_hash: str, since_ms: int,
                            judged_since_ms: int, exclude_id: int,
                            reasons: Sequence[str]) -> int:
        """Claims of an owner (coldkey, else hotkey) on one page and type, received since
        `since_ms` and judged since `judged_since_ms` with one of `reasons`."""
    async def claim_changes(self, hotkey: str, after: Tuple[int, int], until_ms: int,
                            limit: int) -> List[Tuple[int, int]]:
        """(claim id, changed ms) of a hotkey's claims changed after the (ms, id) cursor and
        before `until_ms`, oldest change first."""
    async def claim_heads(self, status: str, limit: int = 500,
                          skip: Sequence[int] = ()) -> List[ClaimHead]: ...
    async def claims_ahead(self, claim: ClaimRow, since_ms: int) -> int: ...
    async def budget_use(self, hotkey: str, since_ms: int) -> Tuple[int, Optional[int]]: ...
    async def queued_hotkeys(self) -> List[str]: ...
    async def admissible_claims(self, limits: Dict[str, int], since_ms: int,
                                limit: int) -> List[ClaimRow]: ...
    async def hotkey_stats(self, since_ms: int,
                           hotkeys: Optional[Iterable[str]] = None) -> Dict[str, HotkeyStats]: ...
    async def scoop_record(self, since_ms: int,
                           owners: Iterable[str]) -> Dict[str, Tuple[int, int]]: ...
    async def bucket_claim_ids(self, buckets: Iterable[int]) -> Dict[int, List[int]]: ...
    async def tie_seeds(self, buckets: Iterable[int]) -> Dict[int, Tuple[int, str]]: ...
    async def put_tie_seed(self, bucket: int, block: int, block_hash: str) -> None: ...
    async def take_jobs(self, kinds: Sequence[str], limit: int, lease_ms: int,
                        now_ms: int, skip_hotkeys: Sequence[str] = (),
                        per_hotkey: int = 0) -> List[Job]:
        """Due jobs, each hotkey's oldest claims first. With `per_hotkey`, the oldest claims
        across hotkeys go first, up to that many per hotkey; without, hotkeys take turns."""
    async def finish_job(self, job_id: int, error: Optional[str] = None) -> None: ...
    async def defer_job(self, job_id: int, run_ms: int, retry: bool = False) -> None: ...
    async def add_job(self, kind: str, claim_id: Optional[int], run_ms: int) -> Job: ...
    async def add_job_once(self, kind: str, claim_id: int, run_ms: int) -> Optional[Job]: ...
    async def append_ledger(self, row: LedgerRow) -> LedgerRow: ...
    async def ledger_rows(self, epochs: Sequence[int]) -> List[LedgerRow]: ...
    async def claim_ledger(self, claim_id: int) -> List[LedgerRow]: ...
    async def claim_points(self, claim_id: int) -> Dict[str, float]: ...
    async def claims_for_event(self, event_id: int, statuses: Sequence[str]) -> List[ClaimRow]: ...
    async def claims_to_settle(self, now_ms: int, limit: int = 200) -> List[ClaimRow]: ...
    async def update_event(self, event_id: int, **fields) -> None: ...
    async def add_facts(self, rows: Sequence[EventFactRow]) -> None: ...
    async def event_facts(self, event_id: int) -> List[EventFactRow]: ...
    async def update_facts(self, claim_id: int, status: str,
                           fields: Optional[Sequence[str]] = None) -> None: ...
    async def add_scoop_candidate(self, row: ScoopCandidateRow) -> Optional[ScoopCandidateRow]: ...
    async def scoop_candidates(self, scoop_id: int,
                               statuses: Optional[Sequence[str]] = None
                               ) -> List[ScoopCandidateRow]: ...
    async def update_scoop_candidate(self, candidate_id: int, **fields) -> None: ...
    async def add_stage(self, event_id: int, stage: str, claim_id: Optional[int],
                        at_ms: int) -> bool: ...
    async def get_attestation(self, signer: str, epoch: int) -> Optional[dict]: ...
    async def put_attestation(self, signer: str, epoch: int, body: dict) -> None: ...
    async def put_reverify(self, row: ReverifyRow) -> None: ...
    async def add_reverify_pull(self, validator: str, epoch: int, at_ms: int) -> None: ...
    async def reverify_claims(self, validator: str, epoch: int) -> set: ...
    async def add_reverify_report(self, validator: str, epoch: int, at_ms: int) -> None: ...
    async def reverify_reports(self, epochs: Sequence[int]) -> Dict[Tuple[str, int], int]: ...
    async def last_attestation_epoch(self, signer: str) -> Optional[int]: ...
    async def get_event(self, event_id: int) -> Optional[EventRecord]: ...
    async def event_by_identity(self, keys: Sequence[str],
                                fingerprint: str) -> Optional[EventRecord]: ...
    async def event_candidates(self, date_lo: date, date_hi: date, actor_keys: Sequence[str],
                               event_type: str, event_date: date) -> List[EventRecord]: ...
    async def add_event(self, rec: EventRecord) -> EventRecord: ...
    async def prize_docs(self, since_ms: int) -> List[str]: ...
    async def add_source_fetch(self, row: SourceFetchRow) -> SourceFetchRow: ...
    async def update_source_fetch(self, fetch_id: int, **fields) -> None: ...
    async def source_fetches(self, claim_id: int) -> List[SourceFetchRow]: ...
    async def fetches_since(self, since_ms: int, limit: int = 100_000) -> List[SourceFetchRow]: ...
    async def held_claims(self, limit: int = 500) -> List[ClaimRow]: ...
    async def add_judge_item(self, row: JudgeItemRow) -> JudgeItemRow: ...
    async def judge_item(self, key: str) -> Optional[JudgeItemRow]: ...
    async def judge_items(self, status: str = "pending",
                          limit: int = 500) -> List[JudgeItemRow]: ...
    async def update_judge_item(self, item_id: int, status: str) -> None: ...
    async def judge_items_count(self, hotkey: str, since_ms: int) -> int: ...
    async def add_judge_post(self, row: JudgePostRow) -> Optional[JudgePostRow]: ...
    async def judge_posts(self, item_ids: Sequence[int]) -> List[JudgePostRow]: ...
    async def judge_verdict(self, key: str) -> Optional[dict]: ...
    async def add_judge_verdict(self, key: str, claim_id: Optional[int], record: dict) -> None: ...
    async def claims_by_source(self, source_id: int, statuses: Sequence[str]) -> List[ClaimRow]: ...
    async def sources(self, domain: Optional[str] = None) -> List[SourceRow]: ...
    async def get_source(self, source_id: int) -> Optional[SourceRow]: ...
    async def add_source(self, row: SourceRow) -> SourceRow: ...
    async def update_source(self, source_id: int, **fields) -> None: ...
    async def get_owner(self, owner_id: str) -> Optional[OwnerRow]: ...
    async def put_owner(self, row: OwnerRow) -> None: ...
    async def source_trust(self, source_id: Optional[int] = None) -> List[SourceTrustRow]: ...
    async def put_source_trust(self, rows: Sequence[SourceTrustRow]) -> None: ...
    async def add_source_event(self, row: SourceEventRow) -> SourceEventRow: ...
    async def source_events(self, source_id: Optional[int] = None,
                            event_id: Optional[int] = None) -> List[SourceEventRow]: ...
    async def page_rules(self) -> List[PageRuleRow]: ...
    async def add_page_rule(self, row: PageRuleRow) -> PageRuleRow: ...
    async def add_trust_log(self, row: TrustLogRow) -> None: ...
    async def trust_log(self, source_id: Optional[int] = None, actor: Optional[str] = None,
                        since_ms: Optional[int] = None) -> List[TrustLogRow]: ...
    async def add_answer_items(self, rows: Sequence[AnswerKeyRow]) -> List[AnswerKeyRow]: ...
    async def answer_items(self, statuses: Sequence[str], observed_since_ms: Optional[int] = None,
                           limit: int = 500) -> List[AnswerKeyRow]: ...
    async def update_answer_item(self, item_id: int, **fields) -> None: ...
    async def add_accuracy_check(self, row: AccuracyCheckRow) -> bool: ...
    async def accuracy_checks(self, since_ms: int, hotkeys: Optional[Iterable[str]] = None,
                              category: Optional[str] = None) -> List[AccuracyCheckRow]: ...
    async def put_miner_accuracy(self, rows: Sequence[MinerAccuracyRow]) -> None: ...
    async def miner_accuracy(self, hotkey: Optional[str] = None) -> List[MinerAccuracyRow]: ...
    async def claims_by_settle(self, since_ms: int, until_ms: int,
                               statuses: Sequence[str]) -> List[ClaimRow]: ...
    async def event_stages(self, event_id: int) -> List[Tuple[str, Optional[int], int]]: ...
    async def list_events(self, q: EventQuery) -> List[EventRecord]: ...
    async def add_feed(self, row: FeedRow) -> FeedRow: ...
    async def feed(self, after_seq: int = 0, until_ms: Optional[int] = None,
                   event_id: Optional[int] = None, category: Optional[str] = None,
                   event_type: Optional[str] = None, entity_keys: Sequence[str] = (),
                   limit: int = 500) -> List[FeedRow]: ...
    async def api_key_owner(self, key_hash: str) -> Optional[str]: ...
    async def add_api_key(self, key_hash: str, owner_id: str) -> None: ...
    async def revoke_api_keys(self, owner_id: Optional[str] = None,
                              key_hash: Optional[str] = None) -> int: ...
    async def registry_entities(self) -> List[EntityRecord]: ...
    async def put_entity(self, record: EntityRecord) -> None: ...
    async def miner_stats(self, since_ms: int, until_ms: int,
                          category: Optional[str] = None) -> Dict[str, MinerStats]: ...
    async def claims_page(self, until_ms: int, hotkey: Optional[str] = None,
                          route: Optional[str] = None, status: Optional[str] = None,
                          category: Optional[str] = None, since_ms: Optional[int] = None,
                          before_id: Optional[int] = None, limit: int = 50,
                          event_type: Optional[str] = None,
                          reason: Optional[str] = None) -> List[ClaimRow]: ...
    async def banded_claims(self, since_ms: int) -> List[ClaimRow]: ...
    async def claims_of_types(self, since_ms: int,
                              event_types: Sequence[str]) -> List[ClaimRow]: ...
    async def ledger_for_claims(self, claim_ids: Sequence[int]) -> List[LedgerRow]: ...
    async def reverify_for_claims(self, claim_ids: Sequence[int]) -> List[ReverifyRow]: ...
    async def fetches_for_claims(self, claim_ids: Sequence[int]) -> List[SourceFetchRow]: ...
    async def claims_by_ids(self, claim_ids: Sequence[int]) -> List[ClaimRow]: ...
    async def judge_items_for_claims(self, claim_ids: Sequence[int]) -> List[JudgeItemRow]: ...
    async def judge_posts_since(self, since_ms: int) -> List[Tuple[int, JudgePostRow]]: ...
    async def candidates_for_scoops(self, scoop_ids: Sequence[int]) -> List[ScoopCandidateRow]: ...
    async def reverify_reports_since(self, since_ms: int) -> Dict[Tuple[str, int], int]: ...
    async def reverify_in_epochs(self, epochs: Sequence[int]) -> List[ReverifyRow]: ...
