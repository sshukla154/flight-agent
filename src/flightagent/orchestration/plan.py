"""Deterministic task planning for one origin (T24).

Master plan S3 places ``plan`` first in the ``orchestration`` package for a
reason: the task set for a run must be derivable from config alone, with no
network call and no randomness. This module builds that plan for exactly
ONE origin against all 8 destinations from ``airports.registry`` — the
run-level, ten-origin fan-out (``MultiOriginSearchRequest`` in
``domain/run.py``) is a separate, later concern that calls this function
once per origin; it is not built here.

Each ``SearchRequest`` is built with the identical defaults ``cli.py``'s
``run()`` command already uses (D3: economy cabin, 1 adult, EUR; layover
bounds from config) so a single-origin plan built here and a single
``flightagent run`` invocation for the same (origin, destination) pair are
requesting the exact same thing.
"""

from __future__ import annotations

from datetime import date, timedelta

from flightagent.airports.registry import UnknownAirportError
from flightagent.airports.registry import destinations as registry_destinations
from flightagent.airports.registry import get as get_airport
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings
from flightagent.domain.enums import CabinClass, StopMode
from flightagent.domain.ids import compute_task_id
from flightagent.domain.run import SearchRequest, SearchTask
from flightagent.observability.events import EventName
from flightagent.observability.logging import log_event

_DEFAULT_ADULTS = 1
"""D3: 1 adult, 0 children, 0 infants — matches ``cli.py``'s own constant."""

_DEFAULT_CABIN = CabinClass.ECONOMY
"""D3 scopes this project to economy only — matches ``cli.py``'s own constant."""

_DEFAULT_CURRENCY = "EUR"
"""D14: every itinerary's price is already reported in this currency —
matches ``cli.py``'s own constant."""


def _origin_priority(origin: str) -> int:
    """Look up ``origin``'s priority from the registry — never hardcoded.

    Raises ``ValueError`` for an IATA code that either does not exist at
    all, or exists only as a destination (no ``priority``, since only the
    10 European origins carry one — see ``Airport.is_origin``). Both are
    caller errors: this project's origin set is closed and comes from
    ``config/ground_access.yaml`` alone.
    """
    try:
        airport = get_airport(origin)
    except UnknownAirportError as exc:
        raise ValueError(f"{origin!r} is not a known airport (see airports.registry)") from exc
    if airport.priority is None:
        raise ValueError(
            f"{origin!r} is not a known ORIGIN airport (it has no priority/ground entry in "
            f"config/ground_access.yaml) — see airports.registry.origins()"
        )
    return airport.priority


def _build_tasks_for_mode(
    origin_code: str,
    *,
    departure_date: date,
    max_stops: StopMode,
    origin_priority: int,
    layover_min: timedelta,
    layover_max: timedelta,
) -> tuple[SearchTask, ...]:
    """The core per-mode task-building loop, shared by ``build_plan_for_origin``
    (single mode) and ``build_dual_mode_plan_for_origin`` (T29, both modes).

    Deliberately does NOT emit ``EventName.PLAN_BUILT`` itself -- both
    public functions below own emitting exactly one such event each, with
    the ``task_count`` that is actually correct for what THEY return (8 for
    a single mode, 16 for both) -- calling this helper twice from the
    dual-mode function must never silently emit two separate 8-task events
    where callers expect one combined 16-task event.
    """
    return tuple(
        SearchTask(
            task_id=compute_task_id(origin_code, destination.iata, max_stops),
            request=SearchRequest(
                origin=origin_code,
                destination=destination.iata,
                departure_date=departure_date,
                cabin=_DEFAULT_CABIN,
                max_stops=max_stops,
                adults=_DEFAULT_ADULTS,
                currency=_DEFAULT_CURRENCY,
                layover_min=layover_min,
                layover_max=layover_max,
            ),
            origin_priority=origin_priority,
            wave=0,
        )
        for destination in registry_destinations()
    )


def build_plan_for_origin(
    origin: str,
    *,
    departure_date: date,
    max_stops: StopMode,
    settings: FlightAgentSettings | None = None,
) -> tuple[SearchTask, ...]:
    """Build the ordered tuple of ``SearchTask``s for ``origin`` -> every
    destination in ``airports.registry.destinations()``, at exactly ONE
    ``max_stops`` mode.

    ``settings`` defaults to ``load_config()`` (the real four-layer config)
    when omitted — matching how ``cli.py``'s ``run()`` command resolves
    config — but callers (tests, a future orchestrator wiring layer that
    already loaded settings once for the whole run) may pass an
    already-resolved ``FlightAgentSettings`` instead, so config is not
    reloaded once per origin in a multi-origin run.

    Every task is ``wave=0`` — Phase 4 has no wave-based early-stop logic
    (that is Phase 6); this plan is a flat, single-wave list.

    Emits exactly one ``EventName.PLAN_BUILT`` event, after the full tuple
    is built, carrying the final ``task_count``.
    """
    resolved_settings = settings if settings is not None else load_config()
    origin_code = origin.upper()
    origin_priority = _origin_priority(origin_code)

    layover_min = timedelta(minutes=resolved_settings.layover.layover_min_minutes)
    layover_max = timedelta(minutes=resolved_settings.layover.layover_max_minutes)

    tasks = _build_tasks_for_mode(
        origin_code,
        departure_date=departure_date,
        max_stops=max_stops,
        origin_priority=origin_priority,
        layover_min=layover_min,
        layover_max=layover_max,
    )

    log_event(EventName.PLAN_BUILT, task_count=len(tasks))
    return tasks


def build_dual_mode_plan_for_origin(
    origin: str,
    *,
    departure_date: date,
    settings: FlightAgentSettings | None = None,
) -> tuple[SearchTask, ...]:
    """Build the ordered tuple of ``SearchTask``s for ``origin`` -> every
    destination, searched at BOTH ``max_stops`` modes (Phase 5, T29 /
    Addendum 1): 8 destinations x {0, 1} = 16 tasks, direct-mode tasks
    first then one-stop-mode tasks.

    Addendum 1 requires searching both a direct-only (``max_stops=0``) and
    an at-most-one-stop (``max_stops=1``) plan for every destination, so
    the later direct-vs-stop policy comparison (D10, T31) has both a
    genuine direct pool and a genuine one-stop pool to compare per
    destination — a single ``max_stops=1`` search alone cannot supply this,
    because a provider has no obligation to surface every direct option
    inside a broader one-stop search (D13 only guarantees the REVERSE: a
    direct itinerary a ``max_stops=1`` search happens to return is valid,
    not that every real direct option will turn up unasked).

    Internally calls the same per-mode task-building logic
    ``build_plan_for_origin`` uses (via ``_build_tasks_for_mode``) once per
    mode and concatenates the two 8-task tuples — never two separate
    ``build_plan_for_origin`` calls, which would each emit their own
    8-task ``PLAN_BUILT`` event. Emits exactly ONE ``EventName.PLAN_BUILT``
    event for the combined 16-task plan.
    """
    resolved_settings = settings if settings is not None else load_config()
    origin_code = origin.upper()
    origin_priority = _origin_priority(origin_code)

    layover_min = timedelta(minutes=resolved_settings.layover.layover_min_minutes)
    layover_max = timedelta(minutes=resolved_settings.layover.layover_max_minutes)

    direct_tasks = _build_tasks_for_mode(
        origin_code,
        departure_date=departure_date,
        max_stops=0,
        origin_priority=origin_priority,
        layover_min=layover_min,
        layover_max=layover_max,
    )
    one_stop_tasks = _build_tasks_for_mode(
        origin_code,
        departure_date=departure_date,
        max_stops=1,
        origin_priority=origin_priority,
        layover_min=layover_min,
        layover_max=layover_max,
    )
    tasks = direct_tasks + one_stop_tasks

    log_event(EventName.PLAN_BUILT, task_count=len(tasks))
    return tasks
