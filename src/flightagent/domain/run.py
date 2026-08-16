"""Run-level and provider-facing request/task/outcome models.

D1: the provider-facing search stays single-origin; ``MultiOriginSearchRequest``
is a separate run-level PROPOSAL layered above it, because Addendum 2's own
multi-origin request schema is truncated mid-sentence in the source spec —
there is nothing there to conform to. Fan-out from one to the other lives in
the planner (orchestration/plan.py, out of scope here); no provider adapter
ever sees more than one origin.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from flightagent.domain.airport import IataCode
from flightagent.domain.enums import CabinClass, RejectionCode, RunStatus, StopMode, TaskState
from flightagent.domain.ground import GroundLeg
from flightagent.domain.money import CurrencyCode, Money


class SearchRequest(BaseModel):
    """One provider-facing, single-origin search request (D1/D2/D3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: IataCode
    destination: IataCode
    departure_date: date
    cabin: CabinClass
    max_stops: StopMode
    adults: int = Field(default=1, ge=1)
    currency: CurrencyCode
    trip_type: Literal["one_way"] = "one_way"
    layover_min: timedelta
    layover_max: timedelta

    @model_validator(mode="after")
    def _validate_layover_bounds(self) -> Self:
        if self.layover_min > self.layover_max:
            raise ValueError(
                f"layover_min ({self.layover_min}) must not exceed layover_max "
                f"({self.layover_max})"
            )
        return self


class OriginCandidate(BaseModel):
    """One origin airport candidate for the run-level fan-out (D1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iata: IataCode
    priority: int = Field(ge=1)
    ground: GroundLeg


class MultiOriginSearchRequest(BaseModel):
    """Run-level, multi-origin search request.

    This is explicitly a PROPOSAL, not a specification: Addendum 2's own
    multi-origin request schema is truncated mid-sentence in the source
    spec (see DECISIONS.md D1 and the ``multi_origin_schema_truncated``
    open question), so there is nothing here that restates something the
    spec actually defines. The planner fans this out into single-origin
    ``SearchRequest``s; no provider adapter ever sees this shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origins: tuple[OriginCandidate, ...] = Field(min_length=1)
    destinations: tuple[IataCode, ...] = Field(min_length=1)
    stop_modes: tuple[StopMode, ...] = Field(min_length=1)
    departure_date: date
    cabin: CabinClass
    adults: int = Field(default=1, ge=1)
    currency: CurrencyCode
    max_ground_travel: timedelta
    ground_mode: str
    early_stop_threshold: Money
    search_mode: Literal["full_fanout", "sequential_priority"]


class SearchTask(BaseModel):
    """One planned (origin, destination, stop_mode) unit of work."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    request: SearchRequest
    origin_priority: int = Field(ge=1)
    wave: int = Field(ge=0)


class TaskOutcome(BaseModel):
    """One task's terminal state — the 8-state ``TaskState`` (master plan S4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    state: TaskState
    attempts: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    offer_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejection_counts: dict[RejectionCode, int] = Field(default_factory=dict)
    cache: Literal["hit", "miss", "bypass"]
    error_type: str | None = None
    error_detail: str | None = None

    @model_validator(mode="after")
    def _validate_accepted_bound(self) -> Self:
        if self.accepted_count > self.offer_count:
            raise ValueError(
                f"accepted_count ({self.accepted_count}) cannot exceed offer_count "
                f"({self.offer_count})"
            )
        return self


class OpenQuestion(BaseModel):
    """One machine-readable spec-gap entry (DECISIONS.md "Open questions
    carried into every report"). ``id`` is stable and snake_case — never
    renumbered or renamed, since downstream reports key on it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str
    relates_to: tuple[str, ...] = ()


class RunMeta(BaseModel):
    """Every volatile field, isolated in its own sub-object.

    Master plan S4: this is what lets the e2e golden test diff the rest of
    ``RunEnvelope`` byte-for-byte while excluding ``run_meta``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    host: str


_ERROR_STATES = frozenset({TaskState.PROVIDER_ERROR, TaskState.RATE_LIMITED, TaskState.TIMEOUT})
_ANSWERED_STATES = frozenset({TaskState.OK, TaskState.NO_OFFERS, TaskState.ALL_REJECTED})
"""Every terminal state in which the provider call itself did NOT fail --
as opposed to ``_ERROR_STATES``, which means the provider call failed.
``ALL_REJECTED`` (offers existed, every one failed post-hoc validation) is
just as much "the provider genuinely answered" as ``OK``/``NO_OFFERS`` --
this set is deliberately about whether a task got a real answer, never
about whether that answer contained anything usable. Renamed from
``_SUCCESSFUL_STATES`` (this task's fix): a batch where every task lands in
``ALL_REJECTED`` is a real, plausible production scenario (e.g. a
misconfigured layover window rejecting every offer uniformly) and must
validate as ``RunStatus.NO_RESULTS``, not raise ``pydantic.ValidationError``
from this very validator -- see ``_validate_status`` below."""


class RunEnvelope(BaseModel):
    """Top-level run record.

    ``status`` is finding 0.5's four-way replacement for the spec's single
    ``no_results`` string. The validator below enforces exactly that
    finding's definitions, so a status can never be assigned inconsistently
    with what the task ledger actually shows:

    - COMPLETE: no error-state tasks, >=1 accepted itinerary.
    - PARTIAL: >=1 error-state task, >=1 accepted itinerary.
    - NO_RESULTS: 0 accepted, >=1 task in OK/NO_OFFERS/ALL_REJECTED (i.e.
      >=1 task the provider actually answered, whether or not anything
      usable came of it -- see ``_ANSWERED_STATES``).
    - FAILED: 0 accepted, every task errored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_meta: RunMeta
    status: RunStatus
    task_outcomes: tuple[TaskOutcome, ...] = Field(min_length=1)
    open_questions: tuple[OpenQuestion, ...] = ()
    config_digest: str
    tzdata_version: str

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        total_accepted = sum(outcome.accepted_count for outcome in self.task_outcomes)
        has_errors = any(outcome.state in _ERROR_STATES for outcome in self.task_outcomes)
        all_errored = all(outcome.state in _ERROR_STATES for outcome in self.task_outcomes)
        has_answered = any(outcome.state in _ANSWERED_STATES for outcome in self.task_outcomes)

        if self.status == RunStatus.COMPLETE:
            if has_errors or total_accepted < 1:
                raise ValueError(
                    "status COMPLETE requires zero error-state tasks and >=1 accepted "
                    "itinerary (finding 0.5)"
                )
        elif self.status == RunStatus.PARTIAL:
            if not has_errors or total_accepted < 1:
                raise ValueError(
                    "status PARTIAL requires >=1 error-state task and >=1 accepted "
                    "itinerary (finding 0.5)"
                )
        elif self.status == RunStatus.NO_RESULTS:
            if total_accepted != 0 or not has_answered:
                raise ValueError(
                    "status NO_RESULTS requires zero accepted itineraries and >=1 "
                    "OK/NO_OFFERS/ALL_REJECTED task -- i.e. >=1 task the provider actually "
                    "answered (finding 0.5)"
                )
        elif self.status == RunStatus.FAILED:
            if total_accepted != 0 or not all_errored:
                raise ValueError(
                    "status FAILED requires zero accepted itineraries and every task in "
                    "an error state (finding 0.5)"
                )
        return self
