"""Closed event-name enum and per-event required-field schemas.

Every structured log line in flightagent names itself via `EventName` — a
closed set, never an arbitrary string. `EventName("typo.event")` raises
`ValueError` at runtime, and passing a plain `str` where `EventName` is
expected is a `mypy --strict` error, not a silently-accepted log line nobody
can query for later.

`EVENT_SCHEMAS` maps each event to the minimal pydantic model of fields that
MUST be present on that event — enforced by validation in
`observability.logging.log_event()`, not left to each call site to
remember (master plan S7: "enforced in code rather than by convention").

Every per-event model allows extra fields (`extra="allow"`). This module
pins the REQUIRED minimum only, not an exhaustive shape — most of the
modules that will eventually emit these events (the planner, the provider
adapters, the scorer, ...) don't exist yet. When they land, they add fields
freely; only a required field ever needs a schema change here.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EventName(StrEnum):
    """The closed set of structured-log event names. Master plan S7."""

    RUN_STARTED = "run.started"
    PLAN_BUILT = "plan.built"
    SEARCH_REQUESTED = "search.requested"
    SEARCH_RESPONSE = "search.response"
    SEARCH_RETRY = "search.retry"
    SEARCH_FAILED = "search.failed"
    CACHE_HIT = "cache.hit"
    CACHE_MISS = "cache.miss"
    CACHE_WRITE = "cache.write"
    NORMALIZE_COMPLETED = "normalize.completed"
    VALIDATE_COMPLETED = "validate.completed"
    DEDUP_COMPLETED = "dedup.completed"
    SCORE_COMPLETED = "score.completed"
    RANK_COMPLETED = "rank.completed"
    RANK_DESTINATION_DROPPED = "rank.destination_dropped"
    POLICY_DIRECT_DECISION = "policy.direct_decision"
    EARLYSTOP_EVALUATED = "earlystop.evaluated"
    EARLYSTOP_TRIGGERED = "earlystop.triggered"
    LLM_CALLED = "llm.called"
    LLM_OUTPUT_REJECTED = "llm.output_rejected"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RECORDED = "approval.recorded"
    REPORT_WRITTEN = "report.written"
    RUN_METRICS = "run.metrics"
    RUN_COMPLETED = "run.completed"


class EventFields(BaseModel):
    """Base for per-event required-field schemas.

    `extra="allow"`: this class pins the required minimum, not an exhaustive
    shape — see module docstring. `frozen=True`: a validated fields object
    records a fact about something that already happened; nothing downstream
    should be mutating it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)


class RunStartedFields(EventFields):
    """No fields required yet beyond the auto-injected run_id/ts/event."""


class PlanBuiltFields(EventFields):
    task_count: int


class SearchRequestedFields(EventFields):
    provider: str
    origin: str
    destination: str
    max_stops: int
    attempt: int


class SearchResponseFields(EventFields):
    provider: str
    origin: str
    destination: str
    max_stops: int
    attempt: int
    http_status: int
    response_bytes: int
    duration_ms: int
    offer_count: int
    cache: str


class SearchRetryFields(EventFields):
    provider: str
    origin: str
    destination: str
    attempt: int
    reason: str


class SearchFailedFields(EventFields):
    provider: str
    origin: str
    destination: str
    attempts: int
    error: str


class CacheHitFields(EventFields):
    provider: str
    origin: str
    destination: str
    cache_key: str


class CacheMissFields(EventFields):
    provider: str
    origin: str
    destination: str
    cache_key: str


class CacheWriteFields(EventFields):
    provider: str
    origin: str
    destination: str
    cache_key: str
    ttl_seconds: int


class NormalizeCompletedFields(EventFields):
    input_count: int
    output_count: int


class ValidateCompletedFields(EventFields):
    """Master plan S7: "validate.completed carries accepted_count + a rejection_counts map"."""

    accepted_count: int
    rejection_counts: dict[str, int]


class DedupCompletedFields(EventFields):
    input_count: int
    output_count: int
    duplicate_count: int


class ScoreCompletedFields(EventFields):
    itinerary_count: int


class RankCompletedFields(EventFields):
    """Master plan S7: rank.completed carries the final ordering as

    (rank, itinerary_id, adjusted_score, price_eur) tuples.
    """

    rankings: list[tuple[int, str, float, float]]


class RankDestinationDroppedFields(EventFields):
    """Non-blocking gap mitigation (Phase 4 fix): the global top-N cut
    (``config.output.top_n_global``) discarded EVERY accepted itinerary for
    one or more destinations that DID have at least one valid, accepted
    itinerary before truncation -- indistinguishable, in the final report,
    from a destination that was never searched at all. Full per-destination
    visibility is Phase 5/6 scope (see ``reporting.markdown``'s own
    docstring); this event exists only so the gap is OBSERVABLE in logs
    rather than completely silent (this project's "never silently discard"
    principle), never as a substitute for that later fix.
    """

    destinations: list[str]
    total_accepted: int
    shown_count: int


class PolicyDirectDecisionFields(EventFields):
    """D10: the direct-tier decision and which threshold fired."""

    destination: str
    tier: str
    tier_reason: str


class EarlystopEvaluatedFields(EventFields):
    """D12: the rule is evaluable only with >=2 completed origins, compared per-destination."""

    destination: str
    origin: str
    compared_against: list[str]
    triggered: bool


class EarlystopTriggeredFields(EventFields):
    destination: str
    origin: str
    compared_against: list[str]
    savings_eur: float


class LlmCalledFields(EventFields):
    role: str
    model: str


class LlmOutputRejectedFields(EventFields):
    role: str
    reason: str


class ApprovalRequestedFields(EventFields):
    reason: str


class ApprovalRecordedFields(EventFields):
    decision: str
    reason: str


class ReportWrittenFields(EventFields):
    path: str
    itinerary_count: int


class RunMetricsFields(EventFields):
    """Master plan S7: cache hit ratio and count of tasks not in OK state."""

    cache_hit_ratio: float
    non_ok_task_count: int


class RunCompletedFields(EventFields):
    """Finding 0.5: one of COMPLETE, PARTIAL, NO_RESULTS, FAILED."""

    status: str
    duration_ms: int


EVENT_SCHEMAS: dict[EventName, type[EventFields]] = {
    EventName.RUN_STARTED: RunStartedFields,
    EventName.PLAN_BUILT: PlanBuiltFields,
    EventName.SEARCH_REQUESTED: SearchRequestedFields,
    EventName.SEARCH_RESPONSE: SearchResponseFields,
    EventName.SEARCH_RETRY: SearchRetryFields,
    EventName.SEARCH_FAILED: SearchFailedFields,
    EventName.CACHE_HIT: CacheHitFields,
    EventName.CACHE_MISS: CacheMissFields,
    EventName.CACHE_WRITE: CacheWriteFields,
    EventName.NORMALIZE_COMPLETED: NormalizeCompletedFields,
    EventName.VALIDATE_COMPLETED: ValidateCompletedFields,
    EventName.DEDUP_COMPLETED: DedupCompletedFields,
    EventName.SCORE_COMPLETED: ScoreCompletedFields,
    EventName.RANK_COMPLETED: RankCompletedFields,
    EventName.RANK_DESTINATION_DROPPED: RankDestinationDroppedFields,
    EventName.POLICY_DIRECT_DECISION: PolicyDirectDecisionFields,
    EventName.EARLYSTOP_EVALUATED: EarlystopEvaluatedFields,
    EventName.EARLYSTOP_TRIGGERED: EarlystopTriggeredFields,
    EventName.LLM_CALLED: LlmCalledFields,
    EventName.LLM_OUTPUT_REJECTED: LlmOutputRejectedFields,
    EventName.APPROVAL_REQUESTED: ApprovalRequestedFields,
    EventName.APPROVAL_RECORDED: ApprovalRecordedFields,
    EventName.REPORT_WRITTEN: ReportWrittenFields,
    EventName.RUN_METRICS: RunMetricsFields,
    EventName.RUN_COMPLETED: RunCompletedFields,
}
