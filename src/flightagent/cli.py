"""``flightagent`` CLI entry point (T16) -- wires T9-T15 into one command.

This is the literal Phase 2 exit criterion: a single ``flightagent run``
invocation that builds a ``SearchRequest`` from CLI flags, calls
``MockProvider.search()`` (T10), normalizes every returned ``RawOffer``
(T11), validates each ``NormalizedItinerary`` (T12), deduplicates the valid
ones by itinerary shape key (T20, finding 0.2), scores the survivors
(T13), ranks them (T14), and writes both v1 report artifacts (T15).

Master plan S1.4's canonical loop order is "validate -> dedup -> score ->
rank" -- T20 inserts the dedup step here, between the existing validate and
score steps, operating only on the itineraries that already passed
validation. Deduping before scoring means a scored/ranked itinerary's
``duplicate_count``/``also_offered_by``/``fare_options`` are already final
by the time scoring sees it, and a codeshare quartet is never scored (and
never occupies four ranked-list slots) as four separate entries.

**Determinism, not wall-clock, for every timestamp this command emits.**
``normalize.builder.build_normalized_itinerary`` requires a caller-supplied
``fare_as_of``, and both ``reporting.markdown.render_markdown_report`` and
``reporting.json_report.build_results_document`` require a caller-supplied
``generated_at`` -- both values are rendered directly into the artifacts
(the JSON's top-level ``generated_at`` key, every itinerary's
``fare_as_of``, and the Markdown summary's "Report generated ..." line).
Master plan S5's own "one subtle determinism trap" is exactly this shape of
bug applied to the mock generator's RNG seed; the identical trap exists
here for wall-clock time. Calling ``datetime.now()`` at this layer would
make two back-to-back runs of the identical command produce two
byte-different artifacts, which the phase's own exit criterion (two runs,
byte-identical output) forbids outright. ``_deterministic_as_of`` below
reuses the mock generator's own request-derived seed (T10's
``compute_seed``) to produce a stable, request-scoped instant instead --
never "now", so identical CLI args always produce identical files. There is
no real "instant this fare was retrieved" to report for synthetic data
anyway (the ``SYNTHETIC DATA`` banner already says so), so a deterministic
stand-in is the honest choice available at this layer, not a compromise on
top of a real one.

D6: only ``--provider mock`` is wired up. Any other provider name raises
``ProviderNotConfigured`` (a ``ProviderConfigError`` subclass, T9's own
taxonomy) rather than silently falling back to mock -- Amadeus/Duffel ship
interface-complete but uncredentialed in Phase 7, and there is no adapter
of either name registered in this codebase yet for Phase 2 to fall back
onto.

Exit code discipline (anticipating, but not building, Phase 4's full
``no_results``/``RunStatus`` contract -- finding 0.5): exit ``0`` only when
at least one itinerary validated, was ranked, and both artifacts were
written. Zero valid itineraries is a nonzero exit and writes NOTHING to
``out/`` -- silently emitting an empty-looking report and claiming success
would be worse than a loud failure. The rejection information collected
while filtering (``ValidationResult.rejections``) is not discarded in that
path; it is summarized (a rejection-code histogram) into the error message
this command prints, so a human immediately sees *why* nothing validated
instead of just *that* nothing did.

T19: that same histogram (plus the accepted count) is also emitted as one
``EventName.VALIDATE_COMPLETED`` structured log line right after the
normalize+validate step, via ``validation.engine.summarize_validation_results``
-- unconditionally, whether or not any itinerary ended up valid, so the
rejection breakdown is queryable from logs even on the zero-valid exit path.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import socket
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, NamedTuple

import typer

from flightagent.config.loader import compute_config_digest, load_config
from flightagent.config.models import FlightAgentSettings
from flightagent.domain.enums import CabinClass, RejectionCode, RunStatus, StopMode, TaskState
from flightagent.domain.ids import generate_run_id
from flightagent.domain.itinerary import NormalizedItinerary, RawOffer
from flightagent.domain.policy import DestinationAnalysis
from flightagent.domain.run import _ERROR_STATES as _TASK_ERROR_STATES
from flightagent.domain.run import RunEnvelope, RunMeta, SearchRequest, SearchTask, TaskOutcome
from flightagent.domain.scoring import ScoredItinerary
from flightagent.domain.validation import ValidationResult
from flightagent.normalize.builder import build_normalized_itinerary
from flightagent.normalize.dedup import deduplicate
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event, setup_logging
from flightagent.orchestration.executor import execute_plan
from flightagent.orchestration.plan import build_dual_mode_plan_for_origin
from flightagent.policy.direct_vs_stop import analyze_destination
from flightagent.providers.base import CallBudget, FlightProvider
from flightagent.providers.errors import ProviderNotConfigured
from flightagent.providers.mock.generator import compute_seed
from flightagent.providers.mock.provider import MockProvider
from flightagent.reporting.json_report import build_results_document
from flightagent.reporting.markdown import render_markdown_report
from flightagent.reporting.view import last_segment
from flightagent.reporting.writer import write_report_artifacts
from flightagent.scoring.ranking import rank_itineraries
from flightagent.scoring.score import score_itinerary
from flightagent.validation.engine import summarize_validation_results, validate

app = typer.Typer(
    name="flightagent",
    help="Autonomous flight-search agent (Nieuwegein-area -> India, mock provider, Phase 2).",
    add_completion=False,
    no_args_is_help=True,
)

NO_VALID_ITINERARIES_EXIT_CODE = 1
"""Exit code when zero itineraries survived validation.

T27 (Phase 4's full ``RunStatus``-derived exit-code table, finding 0.5)
reuses this SAME value for ``--all-destinations``'s ``RunStatus.NO_RESULTS``
branch -- both mean exactly the same thing to a caller ("nothing valid was
found"), just reached via two different pipelines (single-destination vs
the full fan-out). ``ALL_DESTINATIONS_FAILED_EXIT_CODE`` below is
deliberately a THIRD, distinct value for ``RunStatus.FAILED`` -- finding
0.5's whole point is that "nothing validated" and "the provider was down"
must never look identical to a caller inspecting only the exit code.
"""

ALL_DESTINATIONS_FAILED_EXIT_CODE = 3
"""Exit code for ``--all-destinations``'s ``RunStatus.FAILED`` branch --
every planned task ended in an error state, zero accepted. Deliberately
skips ``2`` (Click/Typer's own reserved usage-error exit code, e.g. a
``typer.BadParameter`` raised by this module's own CLI validation) so a
usage mistake and a fully-failed search are never confusable by exit code
alone, and deliberately distinct from ``NO_VALID_ITINERARIES_EXIT_CODE``
per this module's own docstring above.
"""

_LAYOVER_REJECTION_CODES = frozenset(
    {RejectionCode.LAYOVER_TOO_SHORT, RejectionCode.LAYOVER_TOO_LONG}
)
"""D19: the only two ``RejectionCode`` values for which the spec's exact
hardcoded ``no_results`` string is honest. Any other dominant rejection
reason under ``RunStatus.NO_RESULTS`` gets an accurate message naming the
real cause instead (finding 0.5)."""

_SPEC_NO_RESULTS_MESSAGE = "No itinerary satisfied the 3–6 hour layover rule."
"""The ORIGINAL spec's own exact hardcoded string (section 9), reproduced
verbatim -- DECISIONS.md D19 and the master plan both quote this exact
sentence, en dash included. Printed only when ``RunStatus.NO_RESULTS`` AND
the dominant rejection code is a layover violation (D19) -- never
unconditionally, which is exactly finding 0.5's complaint about the
original spec."""

_DEFAULT_ADULTS = 1
"""D3: 1 adult, 0 children, 0 infants -- not a CLI flag, the spec takes no
passenger count as input."""

_DEFAULT_CABIN = CabinClass.ECONOMY
"""D3 scopes this project to economy only -- not a CLI flag."""

_DEFAULT_CURRENCY = "EUR"
"""Every itinerary's ``price_eur`` is already the report's native currency
(D14); not a CLI flag in Phase 2."""


@app.callback()
def _callback() -> None:
    """Autonomous flight-search agent (Nieuwegein-area -> India, Phase 2: mock provider only).

    An explicit Typer callback -- with only one subcommand registered
    (``run``), Typer would otherwise collapse this app so that subcommand's
    name is optional (``flightagent --origin ...`` would work without
    ``run``). Registering ANY callback forces Typer to keep ``run`` as a
    required, named subcommand, matching the exact target invocation shape
    (``flightagent run --origin ...``) this phase's exit criterion
    specifies verbatim.

    It also wires up structured logging (T19/Phase 3 gap fix): this
    callback runs exactly once before any subcommand, so it is the natural
    place to call ``setup_logging`` for the real CLI -- previously only
    test code and ``scripts/logging_smoke.py`` ever called it, which meant
    every ``log_event`` call in this module and in
    ``normalize.dedup.deduplicate`` logged into a ``"flightagent"`` logger
    with no attached handler and was silently dropped. Structured JSON
    lines go to stderr, following ``scripts/logging_smoke.py``'s own
    precedent of calling ``setup_logging`` with no destination override in
    a context where stdout is otherwise free -- here stdout is NOT free
    (the ``run`` command's own plain-text summary line goes there), so
    stderr is the explicit destination, keeping the two streams separate.
    """
    setup_logging(stream=sys.stderr)


def _to_stop_mode(value: int) -> StopMode:
    """Narrow a plain ``int`` (already range-checked 0..1 by the
    ``--max-stops`` option's own ``min=0, max=1``) to the ``StopMode``
    literal type ``SearchRequest.max_stops`` requires.

    A second, explicit check here rather than trusting the CLI-level range
    check alone: this is a plain function a future non-Typer caller could
    invoke directly, bypassing Click's own validation entirely.
    """
    if value == 0:
        return 0
    if value == 1:
        return 1
    raise typer.BadParameter("--max-stops must be 0 or 1 (D13: at most one stop)")


def _parse_departure_date(value: str) -> date:
    """Parse ``--date`` as a strict ISO ``YYYY-MM-DD`` date.

    Raises ``typer.BadParameter`` (a clean CLI usage error, exit code 2)
    rather than letting a malformed string reach ``SearchRequest``'s own
    pydantic validation, whose error would be a much less legible
    traceback for what is, at this layer, simple CLI input.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--date must be an ISO YYYY-MM-DD date, got {value!r}"
        ) from exc


def _build_provider(name: str) -> FlightProvider:
    """Resolve ``--provider`` to a ``FlightProvider`` instance.

    D6: only ``"mock"`` ships working in Phase 2. Anything else raises
    ``ProviderNotConfigured`` -- a ``ProviderConfigError`` subclass (T9's
    error taxonomy) -- naming exactly D6's own scenario (Amadeus/Duffel
    ship interface-complete but uncredentialed) even though neither
    adapter class exists in this codebase yet to actually import. This
    must never silently fall back to ``MockProvider`` for an unrecognised
    name -- that would make a typo'd ``--provider`` name look like it
    searched a real provider when it did not.
    """
    if name == "mock":
        return MockProvider()
    raise ProviderNotConfigured(
        f"provider {name!r} is not configured -- Phase 2 ships mock-only (D6); "
        f"only '--provider mock' is wired up. Real adapters (Amadeus, Duffel) land in "
        f"Phase 7 and will raise this same error at runtime until credentials are supplied.",
        provider=name,
    )


_AS_OF_LOOKBACK = timedelta(days=90)
"""Width of the deterministic window ``_deterministic_as_of`` places its
result in -- always strictly before the requested departure date, never
after. Only affects how plausible the reported "fare retrieved" instant
reads; the determinism property does not depend on this specific width."""


def _deterministic_as_of(request: SearchRequest) -> datetime:
    """A deterministic, request-scoped UTC instant -- reused for both
    ``fare_as_of`` (T11) and ``generated_at`` (T15) so that two runs of an
    identical CLI invocation produce byte-identical artifacts.

    Reuses T10's own ``compute_seed`` (a sha256 over the request's
    canonical search fields) rather than re-deriving a second hash formula
    -- one seed function for "this request, deterministically" is
    plenty, and reusing it here means this value changes exactly when the
    mock generator's own offers would, never independently. See this
    module's docstring for why "now" is wrong at this layer.

    The result always falls somewhere in the ``_AS_OF_LOOKBACK`` window
    immediately before midnight UTC on ``request.departure_date`` -- a
    "fare last checked shortly before departure" instant is what a real
    caller would report, so a deterministic stand-in should land somewhere
    plausible too, not on an arbitrary epoch offset that could land decades
    away from the trip it is dated against.
    """
    seed = compute_seed(request)
    offset_seconds = seed % int(_AS_OF_LOOKBACK.total_seconds())
    window_start = datetime.combine(request.departure_date, time.min, tzinfo=UTC) - _AS_OF_LOOKBACK
    return window_start + timedelta(seconds=offset_seconds)


def _normalize_and_validate(
    request: SearchRequest,
    raw_offers: tuple[RawOffer, ...],
    *,
    as_of: datetime,
) -> tuple[list[NormalizedItinerary], list[ValidationResult]]:
    """Run every ``RawOffer`` through T11 (normalize) then T12 (validate).

    Returns ``(valid_itineraries, validation_results)`` -- every
    ``ValidationResult`` produced is kept, valid AND invalid alike, so the
    caller can derive both the zero-valid error breakdown and the
    ``EventName.VALIDATE_COMPLETED`` accepted_count/rejection_counts pair
    (T19, ``validation.engine.summarize_validation_results``) from one
    shared pass instead of validating the batch twice.
    """
    valid_itineraries: list[NormalizedItinerary] = []
    validation_results: list[ValidationResult] = []
    for raw_offer in raw_offers:
        itinerary = build_normalized_itinerary(
            raw_offer,
            adults=request.adults,
            cabin=request.cabin,
            fare_as_of=as_of,
        )
        validation_result = validate(itinerary, request)
        validation_results.append(validation_result)
        if validation_result.is_valid:
            valid_itineraries.append(itinerary)
    return valid_itineraries, validation_results


def _to_rejection_code_counts(raw: dict[str, int]) -> dict[RejectionCode, int]:
    """Convert ``summarize_validation_results``'s ``dict[str, int]`` (keyed
    by the plain ``RejectionCode.value`` string, matching
    ``observability.events.ValidateCompletedFields``'s own schema) into the
    ``dict[RejectionCode, int]`` shape ``TaskOutcome.rejection_counts``
    (domain/run.py) is actually typed as.

    This conversion has to happen explicitly: ``TaskOutcome`` is frozen and
    gets finalized here via ``model_copy``, which -- per
    ``normalize.dedup``'s own docstring on the identical point -- does NOT
    re-run pydantic validators. Handing ``model_copy`` a plain string-keyed
    dict would silently leave the finalized ``TaskOutcome`` holding
    ``str`` keys instead of ``RejectionCode`` members, which still happens
    to compare equal (``RejectionCode`` is a ``StrEnum``) but breaks the
    first real attribute access that assumes an actual enum member (e.g.
    ``RejectionCode.value``, used by ``_dominant_rejection_code`` below).
    """
    return {RejectionCode(code): count for code, count in raw.items()}


def _finalize_task_outcome(
    outcome: TaskOutcome, *, accepted_count: int, rejection_counts: dict[RejectionCode, int]
) -> TaskOutcome:
    """Finalize one executor-produced, provisional ``TaskOutcome`` (T24/T25)
    with the REAL ``accepted_count``/``rejection_counts`` this CLI layer
    just computed by running that task's offers through normalize+validate.

    T27's own upgrade rule: a task the executor left in ``TaskState.OK``
    ("the provider answered with offers") whose real ``accepted_count``
    came back ``0`` is upgraded to ``TaskState.ALL_REJECTED`` -- offers
    existed but every one failed validation, a genuinely different
    situation from ``TaskState.NO_OFFERS`` (the provider had nothing to
    offer at all). Every other state (``NO_OFFERS``, the three error
    states) is left exactly as the executor produced it -- this function is
    only ever called for ``OK`` tasks in practice (see ``_run_all_destinations``),
    but is written to be a safe no-op-on-state for any other input too.
    """
    upgrade = outcome.state == TaskState.OK and accepted_count == 0
    state = TaskState.ALL_REJECTED if upgrade else outcome.state
    return outcome.model_copy(
        update={
            "state": state,
            "accepted_count": accepted_count,
            "rejection_counts": rejection_counts,
        }
    )


def _dominant_rejection_code(task_outcomes: tuple[TaskOutcome, ...]) -> RejectionCode | None:
    """D19: the single most frequent ``RejectionCode`` across every task's
    ``rejection_counts``, combined -- a plain function over already-modeled
    data, computed here at the CLI layer rather than stored on
    ``RunEnvelope`` itself (see D19's own rationale in DECISIONS.md: a
    ``RunEnvelope`` field would force every constructor to compute this
    even when the status is COMPLETE/PARTIAL, where it is meaningless).

    Returns ``None`` when no task carries any rejection at all (e.g. every
    task is ``NO_OFFERS`` or already in an error state) -- there is no
    "dominant" reason among zero rejections.

    Ties are broken by the code's own string value, descending -- a
    deterministic function of the counts alone, never of task processing
    order (which, under the executor's concurrent fan-out, is not
    reproducible run-to-run).
    """
    totals: dict[RejectionCode, int] = {}
    for outcome in task_outcomes:
        for code, count in outcome.rejection_counts.items():
            totals[code] = totals.get(code, 0) + count
    if not totals:
        return None
    return max(totals.items(), key=lambda item: (item[1], item[0].value))[0]


def _warn_on_truncated_destinations(
    scored_itineraries: Sequence[ScoredItinerary], ranked: Sequence[ScoredItinerary]
) -> None:
    """Non-blocking gap mitigation (Phase 4 fix, D15's ``top_n_global`` cut):
    when the global top-N truncation discards EVERY accepted itinerary for
    a destination that had >=1 valid, accepted itinerary before truncation,
    that destination becomes indistinguishable in the final report from one
    that was never searched at all.

    This does NOT fix that visibility gap -- full per-destination
    visibility (an "Origin Comparison"/"Direct Flight Analysis" section) is
    genuinely Phase 5/6 scope, per ``reporting.markdown``'s own docstring.
    It only makes the gap OBSERVABLE in logs rather than completely silent,
    consistent with this project's repeated "never silently discard"
    principle: one ``EventName.RANK_DESTINATION_DROPPED`` WARNING-level
    event, naming every destination whose itineraries were entirely cut
    plus the total-accepted-vs-shown counts for the whole batch, is emitted
    when (and only when) at least one destination was fully dropped.

    ``scored_itineraries`` is the full, pre-truncation set; ``ranked`` is
    ``rank_itineraries``'s already-truncated output. Both are keyed by each
    itinerary's arrival airport (``reporting.view.last_segment``), the same
    "destination" every other per-destination view in this codebase (e.g.
    ``json_report``'s ``destination`` field) already uses.
    """
    total_by_destination: dict[str, int] = {}
    for item in scored_itineraries:
        destination = last_segment(item.itinerary).destination
        total_by_destination[destination] = total_by_destination.get(destination, 0) + 1

    shown_destinations = {last_segment(item.itinerary).destination for item in ranked}

    dropped = sorted(
        destination
        for destination in total_by_destination
        if destination not in shown_destinations
    )
    if not dropped:
        return

    log_event(
        EventName.RANK_DESTINATION_DROPPED,
        level=logging.WARNING,
        destinations=dropped,
        total_accepted=len(scored_itineraries),
        shown_count=len(ranked),
    )


def _compute_run_status(task_outcomes: tuple[TaskOutcome, ...]) -> RunStatus:
    """Mirror ``RunEnvelope``'s own status validator (domain/run.py) so the
    candidate status handed to its constructor is already consistent by
    that validator's own logic -- per this task's explicit instruction not
    to compute a status independently and then try to force an
    inconsistent one past it.

    - >=1 accepted itinerary anywhere: COMPLETE (no error-state task) or
      PARTIAL (>=1 error-state task).
    - 0 accepted, every task in an error state: FAILED.
    - 0 accepted, otherwise: NO_RESULTS.

    A batch where EVERY task ends in ``ALL_REJECTED`` (no task in
    ``NO_OFFERS``, no task in an error state) is exactly this last
    NO_RESULTS branch, and constructs cleanly: ``RunEnvelope``'s own
    validator now requires NO_RESULTS to have >=1 task in
    ``OK``/``NO_OFFERS``/``ALL_REJECTED`` (``domain.run._ANSWERED_STATES``),
    not ``OK``/``NO_OFFERS`` alone -- previously ``ALL_REJECTED`` was
    excluded from that set, which made this exact all-``ALL_REJECTED``
    combination raise ``pydantic.ValidationError`` from the model itself
    instead of validating as NO_RESULTS. Fixed in domain/run.py; this
    docstring no longer describes a gap.
    """
    total_accepted = sum(outcome.accepted_count for outcome in task_outcomes)
    has_errors = any(outcome.state in _TASK_ERROR_STATES for outcome in task_outcomes)
    all_errored = all(outcome.state in _TASK_ERROR_STATES for outcome in task_outcomes)

    if total_accepted >= 1:
        return RunStatus.PARTIAL if has_errors else RunStatus.COMPLETE
    if all_errored:
        return RunStatus.FAILED
    return RunStatus.NO_RESULTS


def _tzdata_version() -> str:
    """The installed ``tzdata`` PyPI package's own version.

    Not an IANA tzdata release identifier (e.g. ``"2026a"``) -- there is no
    stdlib API exposing that string. This project's tz database source IS
    exactly the pinned PyPI package (``pyproject.toml``: ``tzdata>=2026a``),
    so that package's own installed version is the honest, actually-available
    answer to "which tz database is this process using", not a compromise
    standing in for a real one.
    """
    return importlib.metadata.version("tzdata")


class _DirectVsStopPools(NamedTuple):
    """One destination's direct-mode and one-stop-mode validated-itinerary
    pools (D13, Phase 5 T29) -- built so a later task (T31, not built in
    this task) can compare them per destination without re-deriving which
    ``task_id`` belongs to which mode.

    ``one_stop`` is already filtered to ``stop_count >= 1``: D13 accepts a
    direct-shaped itinerary inside a ``max_stops=1`` search, but that
    itinerary must never be double-counted against the direct pool the
    ``max_stops=0`` search for the SAME destination already found
    independently.
    """

    direct: tuple[NormalizedItinerary, ...] = ()
    one_stop: tuple[NormalizedItinerary, ...] = ()


def _build_direct_vs_stop_pools(
    tasks: tuple[SearchTask, ...],
    valid_by_task_id: dict[str, tuple[NormalizedItinerary, ...]],
) -> dict[str, _DirectVsStopPools]:
    """Per-destination direct/one-stop pools for Phase 5's later
    direct-vs-stop policy comparison (T31) -- D13's pool-separation rule,
    applied at exactly this layer and nowhere else.

    This is a NARROWER, separately-built view. It never touches
    ``combined_valid_itineraries`` (the main ranked report's own input,
    built in ``_run_all_destinations`` just above where this is called) --
    that keeps showing every valid itinerary from BOTH searches, direct
    and one-stop alike, exactly as it already did, unfiltered.
    """
    pools: dict[str, _DirectVsStopPools] = {}
    for task in tasks:
        destination = task.request.destination
        itineraries = valid_by_task_id.get(task.task_id, ())
        existing = pools.get(destination, _DirectVsStopPools())
        if task.request.max_stops == 0:
            pools[destination] = existing._replace(direct=itineraries)
        else:
            one_stop_only = tuple(
                itinerary for itinerary in itineraries if itinerary.stop_count >= 1
            )
            pools[destination] = existing._replace(one_stop=one_stop_only)
    return pools


def _analyze_all_destinations(
    pools_by_destination: dict[str, _DirectVsStopPools], *, settings: FlightAgentSettings
) -> list[DestinationAnalysis]:
    """T33: run the D10 direct-vs-stop policy (``policy.direct_vs_stop.analyze_destination``)
    over every destination in ``pools_by_destination``, one ``DestinationAnalysis``
    each -- the "Direct Flight Analysis" section's (and the JSON's
    ``destination_analyses`` array's) entire data source.

    Iterates ``pools_by_destination`` in its own (dict-insertion) order,
    which ``_build_direct_vs_stop_pools`` builds from ``tasks`` -- direct-mode
    tasks first, one-stop-mode tasks second, both in registry destination
    order -- so this always comes out as all 8 registry destinations, in
    registry order, never re-sorted here and never silently dropping one
    (D15's amendment: this table is the only per-destination visibility
    the global top-N ranking cannot provide).
    """
    return [
        analyze_destination(
            destination,
            direct_pool=pools.direct,
            one_stop_pool=pools.one_stop,
            direct_tier_settings=settings.direct_tier,
            scoring_settings=settings.scoring,
            layover_settings=settings.layover,
        )
        for destination, pools in pools_by_destination.items()
    ]


def _run_all_destinations(
    *,
    origin: str,
    departure_date: date,
    provider_instance: FlightProvider,
    settings: FlightAgentSettings,
) -> None:
    """``--all-destinations``: search ``origin`` against every registry
    destination in BOTH ``max_stops`` modes (Phase 5, T29 / Addendum 1),
    finalize the task ledger, and render the no_results/PARTIAL/FAILED
    status contract (T27, finding 0.5, D19).

    ``--max-stops`` is NOT consulted here (it still governs the single
    ``--dest`` pipeline unchanged): Addendum 1 requires searching a
    destination's direct (``max_stops=0``) AND one-stop (``max_stops=1``)
    plans unconditionally for the direct-vs-stop policy comparison
    (D10/T31), so this path always builds and executes both -- 8
    destinations x 2 modes = 16 tasks (the literal Phase 5 exit criterion).

    Pipeline: ``orchestration.plan.build_dual_mode_plan_for_origin`` (T29)
    -> ``orchestration.executor.execute_plan`` (T24/T25, retry already
    wired in) over all 16 tasks -> per successful task, this module's own
    ``_normalize_and_validate`` (reused unchanged) -> finalize every
    ``TaskOutcome`` (T27's own OK-with-zero-accepted -> ALL_REJECTED
    upgrade) -> dedup the COMBINED valid itineraries across every
    destination AND both modes (T20) -> score (T13) -> rank (T14) ->
    construct a ``RunEnvelope`` and branch on its ``RunStatus`` (finding
    0.5). Separately, ``_build_direct_vs_stop_pools`` builds the D13
    pool-separated per-destination view, and ``_analyze_all_destinations``
    runs the D10 policy (T31/T33, ``policy.direct_vs_stop.analyze_destination``)
    over it -- one ``DestinationAnalysis`` per registry destination, fed
    into both artifacts' "Direct Flight Analysis"/``destination_analyses``
    output. That pass never feeds the ranked report itself (see
    ``_build_direct_vs_stop_pools``' own docstring); it is a separate,
    per-destination view assembled from the same already-validated pools.

    Never raises a bare pydantic error for the ordinary cases (COMPLETE,
    PARTIAL, NO_RESULTS, FAILED) -- see ``_compute_run_status`` for the one
    degenerate task-ledger shape this candidate-status mirror cannot
    always satisfy.
    """
    started_at = datetime.now(UTC)

    origin_code = origin.upper()
    tasks = build_dual_mode_plan_for_origin(
        origin_code, departure_date=departure_date, settings=settings
    )
    execution_results = asyncio.run(execute_plan(tasks, provider_instance, settings=settings))

    finalized_outcomes: list[TaskOutcome] = []
    combined_valid_itineraries: list[NormalizedItinerary] = []
    valid_by_task_id: dict[str, tuple[NormalizedItinerary, ...]] = {}

    for task, execution_result in zip(tasks, execution_results, strict=True):
        outcome = execution_result.outcome
        if outcome.state != TaskState.OK:
            # NO_OFFERS / an error state: no offers exist to normalize or
            # validate, and accepted_count/rejection_counts are already
            # 0/{} from the executor -- nothing to finalize.
            finalized_outcomes.append(outcome)
            continue

        as_of = _deterministic_as_of(task.request)
        valid_itineraries, validation_results = _normalize_and_validate(
            task.request, execution_result.offers, as_of=as_of
        )
        raw_accepted_count, raw_rejection_counts = summarize_validation_results(validation_results)
        log_event(
            EventName.VALIDATE_COMPLETED,
            accepted_count=raw_accepted_count,
            rejection_counts=raw_rejection_counts,
        )
        finalized_outcomes.append(
            _finalize_task_outcome(
                outcome,
                accepted_count=raw_accepted_count,
                rejection_counts=_to_rejection_code_counts(raw_rejection_counts),
            )
        )
        combined_valid_itineraries.extend(valid_itineraries)
        valid_by_task_id[task.task_id] = tuple(valid_itineraries)

    final_task_outcomes = tuple(finalized_outcomes)

    # D13 pool separation (T29) plus the D10 policy comparison itself (T31,
    # T33): built now so both artifacts below can render the "Direct Flight
    # Analysis" section -- this pass does not feed the ranked report at all
    # (see _build_direct_vs_stop_pools' own docstring), it is a separate,
    # per-destination view assembled from the same already-validated pools.
    _direct_vs_stop_pools = _build_direct_vs_stop_pools(tasks, valid_by_task_id)
    destination_analyses = _analyze_all_destinations(_direct_vs_stop_pools, settings=settings)

    deduplicated_itineraries = deduplicate(combined_valid_itineraries)
    scored_itineraries = [
        ScoredItinerary(
            itinerary=itinerary,
            components=score_itinerary(
                itinerary, scoring_settings=settings.scoring, layover_settings=settings.layover
            ),
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )
        for itinerary in deduplicated_itineraries
    ]
    ranked = rank_itineraries(scored_itineraries, top_n=settings.output.top_n_global)
    _warn_on_truncated_destinations(scored_itineraries, ranked)
    accepted_count = len(scored_itineraries)

    dominant_code = _dominant_rejection_code(final_task_outcomes)
    status = _compute_run_status(final_task_outcomes)

    completed_at = datetime.now(UTC)
    duration_ms = int((completed_at - started_at).total_seconds() * 1000)

    run_meta = RunMeta(
        run_id=generate_run_id(),
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        host=socket.gethostname(),
    )
    envelope = RunEnvelope(
        run_meta=run_meta,
        status=status,
        task_outcomes=final_task_outcomes,
        config_digest=compute_config_digest(settings),
        tzdata_version=_tzdata_version(),
    )

    # Distinct destinations, not task count: with dual-mode search (T29)
    # `tasks` holds 16 entries (8 destinations x 2 modes), and every
    # user-facing message below is about how many DESTINATIONS were
    # searched, not how many provider calls that took.
    destination_count = len({task.request.destination for task in tasks})

    if envelope.status in (RunStatus.COMPLETE, RunStatus.PARTIAL):
        # T26's Failed Searches section reads task_outcomes itself: it
        # naturally renders for PARTIAL (>=1 error-state task) and stays
        # empty for COMPLETE (zero error-state tasks) -- no branching
        # needed here beyond passing final_task_outcomes through.
        generated_at = _deterministic_as_of(tasks[0].request)
        markdown = render_markdown_report(
            ranked,
            departure_date=departure_date,
            accepted_count=accepted_count,
            generated_at=generated_at,
            data_source="mock",
            task_outcomes=final_task_outcomes,
            destination_analyses=destination_analyses,
        )
        json_document = build_results_document(
            ranked,
            departure_date=departure_date,
            accepted_count=accepted_count,
            top_n=settings.output.top_n_global,
            generated_at=generated_at,
            data_source="mock",
            task_outcomes=final_task_outcomes,
            destination_analyses=destination_analyses,
        )
        report_path, results_path = write_report_artifacts(
            markdown=markdown,
            json_data=json_document,
            report_path=Path(settings.output.report_path),
            results_path=Path(settings.output.results_path),
        )
        typer.echo(
            f"flightagent: {envelope.status.value} -- {accepted_count} valid itinerary(ies) "
            f"across {destination_count} destination(s) from {origin_code} on "
            f"{departure_date.isoformat()}; wrote {report_path} and {results_path}"
        )
        log_event(EventName.RUN_COMPLETED, status=envelope.status.value, duration_ms=duration_ms)
        return

    if envelope.status == RunStatus.NO_RESULTS:
        if dominant_code in _LAYOVER_REJECTION_CODES:
            # D19 / spec section 9: the ORIGINAL spec's exact hardcoded
            # string, verbatim, and ONLY here -- the one dominant-code case
            # where it happens to be true.
            typer.echo(
                json.dumps(
                    {"status": "no_results", "message": _SPEC_NO_RESULTS_MESSAGE},
                    ensure_ascii=False,
                )
            )
        else:
            reason = dominant_code.value if dominant_code is not None else "no valid offers found"
            typer.echo(
                f"flightagent: no_results -- 0 valid itinerary(ies) across "
                f"{destination_count} destination(s) from {origin_code} on "
                f"{departure_date.isoformat()} -- dominant rejection reason: {reason}. "
                f"No report written.",
                err=True,
            )
        log_event(EventName.RUN_COMPLETED, status=envelope.status.value, duration_ms=duration_ms)
        raise typer.Exit(code=NO_VALID_ITINERARIES_EXIT_CODE)

    # RunStatus.FAILED: every task errored, zero accepted -- finding 0.5's
    # own case the original spec omitted entirely. Must never read like
    # NO_RESULTS's message (that is finding 0.5's whole point): distinct
    # wording, distinct exit code.
    typer.echo(
        f"flightagent: failed -- every search for {origin_code} across "
        f"{destination_count} destination(s) on {departure_date.isoformat()} errored; "
        f"the provider was unreachable (or every attempt otherwise failed), not merely "
        f"short of valid itineraries. No report written.",
        err=True,
    )
    log_event(EventName.RUN_COMPLETED, status=envelope.status.value, duration_ms=duration_ms)
    raise typer.Exit(code=ALL_DESTINATIONS_FAILED_EXIT_CODE)


@app.command()
def run(
    origin: Annotated[
        str, typer.Option("--origin", help="Origin IATA airport code, e.g. AMS.")
    ],
    date_str: Annotated[
        str, typer.Option("--date", help="Departure date, ISO format YYYY-MM-DD.")
    ],
    max_stops: Annotated[
        int,
        typer.Option(
            "--max-stops",
            min=0,
            max=1,
            help="Maximum stops: 0 (direct only) or 1 (at most one stop, D13). Only applies "
            "to --dest; --all-destinations always searches both modes (T29).",
        ),
    ],
    dest: Annotated[
        str | None,
        typer.Option(
            "--dest",
            help="Destination IATA airport code, e.g. DEL. Required unless --all-destinations "
            "is given; mutually exclusive with it.",
        ),
    ] = None,
    provider: Annotated[
        str,
        typer.Option("--provider", help="Provider to search. Only 'mock' works in Phase 2."),
    ] = "mock",
    all_destinations: Annotated[
        bool,
        typer.Option(
            "--all-destinations",
            help="Search --origin against all 8 registry destinations at once (T27) instead "
            "of a single --dest. Mutually exclusive with --dest.",
        ),
    ] = False,
) -> None:
    """Search, validate, dedup, score, rank, and report -- either one
    origin/destination pair (the default) or, with ``--all-destinations``,
    ``--origin`` against every registry destination at once (T27).

    Single-destination pipeline (in order, unchanged since Phase 2): build a
    ``SearchRequest`` -> ``MockProvider.search()`` (T10) -> normalize every
    offer (T11) -> validate every itinerary, keeping only the valid ones
    (T12) -> deduplicate by itinerary shape key (T20) -> score (T13) -> rank
    (T14) -> write both artifacts (T15). Exits ``0`` only if at least one
    itinerary validated and both artifacts were written; exits
    ``NO_VALID_ITINERARIES_EXIT_CODE`` if zero itineraries validated,
    writing nothing to ``out/``. Dedup runs on the already-valid set and can
    only ever reduce or preserve its size, never turn a zero-valid outcome
    into a non-zero one or vice versa.

    ``--all-destinations`` pipeline: see ``_run_all_destinations``'s own
    docstring for the fan-out/finalize/status-branch contract (finding 0.5,
    D19). Exactly one of ``--dest``/``--all-destinations`` must be given --
    both or neither raises ``typer.BadParameter`` (a clean CLI usage error,
    exit code 2) rather than either silently ignoring one or guessing.
    ``--max-stops`` is NOT consulted for the ``--all-destinations`` path
    (T29): that path always searches both modes, per Addendum 1.
    """
    if all_destinations and dest is not None:
        raise typer.BadParameter(
            "--dest and --all-destinations are mutually exclusive -- give exactly one."
        )
    if not all_destinations and dest is None:
        raise typer.BadParameter("--dest is required unless --all-destinations is given.")

    # Resolve the provider FIRST -- a bad --provider value must fail before
    # any config is loaded or any (deterministic, harmless) mock work runs.
    provider_instance = _build_provider(provider)

    settings = load_config()
    departure_date = _parse_departure_date(date_str)
    stop_mode = _to_stop_mode(max_stops)

    if all_destinations:
        _run_all_destinations(
            origin=origin,
            departure_date=departure_date,
            provider_instance=provider_instance,
            settings=settings,
        )
        return

    # Narrows `dest: str | None` -> `str` for every line below: the guard
    # above already raised if `dest` were `None` here (this line is only
    # ever reached when `all_destinations` is False), but mypy cannot trace
    # that cross-branch invariant on its own.
    assert dest is not None

    request = SearchRequest(
        origin=origin.upper(),
        destination=dest.upper(),
        departure_date=departure_date,
        cabin=_DEFAULT_CABIN,
        max_stops=stop_mode,
        adults=_DEFAULT_ADULTS,
        currency=_DEFAULT_CURRENCY,
        layover_min=timedelta(minutes=settings.layover.layover_min_minutes),
        layover_max=timedelta(minutes=settings.layover.layover_max_minutes),
    )

    as_of = _deterministic_as_of(request)

    search_result = asyncio.run(provider_instance.search(request, CallBudget()))
    total_offers = len(search_result.offers)

    valid_itineraries, validation_results = _normalize_and_validate(
        request, search_result.offers, as_of=as_of
    )

    validate_accepted_count, rejection_counts = summarize_validation_results(validation_results)
    log_event(
        EventName.VALIDATE_COMPLETED,
        accepted_count=validate_accepted_count,
        rejection_counts=rejection_counts,
    )

    if not valid_itineraries:
        if rejection_counts:
            breakdown = ", ".join(
                f"{code}={count}" for code, count in sorted(rejection_counts.items())
            )
        else:
            breakdown = "provider returned no offers"
        typer.echo(
            f"flightagent: 0 valid itineraries out of {total_offers} offer(s) for "
            f"{request.origin}->{request.destination} on {request.departure_date} "
            f"-- rejections: {breakdown}. No report written.",
            err=True,
        )
        raise typer.Exit(code=NO_VALID_ITINERARIES_EXIT_CODE)

    deduplicated_itineraries = deduplicate(valid_itineraries)

    scored_itineraries = [
        ScoredItinerary(
            itinerary=itinerary,
            components=score_itinerary(
                itinerary, scoring_settings=settings.scoring, layover_settings=settings.layover
            ),
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )
        for itinerary in deduplicated_itineraries
    ]

    ranked = rank_itineraries(scored_itineraries, top_n=settings.output.top_n_global)
    accepted_count = len(scored_itineraries)

    markdown = render_markdown_report(
        ranked,
        departure_date=request.departure_date,
        accepted_count=accepted_count,
        generated_at=as_of,
        data_source="mock",
    )
    json_document = build_results_document(
        ranked,
        departure_date=request.departure_date,
        accepted_count=accepted_count,
        top_n=settings.output.top_n_global,
        generated_at=as_of,
        data_source="mock",
    )

    report_path, results_path = write_report_artifacts(
        markdown=markdown,
        json_data=json_document,
        report_path=Path(settings.output.report_path),
        results_path=Path(settings.output.results_path),
    )

    typer.echo(
        f"flightagent: {accepted_count} valid itinerary(ies) out of {total_offers} offer(s) "
        f"for {request.origin}->{request.destination} on {request.departure_date}; "
        f"wrote {report_path} and {results_path}"
    )


def main() -> None:
    """Console-script / ``python -m flightagent`` entry point."""
    app()


if __name__ == "__main__":
    main()
