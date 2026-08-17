"""Ground-travel hard filter (D7 / master plan S6) — a PLAN-TIME gate over
origins, never a per-itinerary validation rule and never a scoring input.

Master plan S6, verbatim: "The 2.5-hour ground limit is a constraint, not a
score term — it belongs in the validation engine as GROUND_TRAVEL_EXCEEDED,
evaluated per origin before any task for that origin is planned." That
sentence's own conclusion — "before any task for that origin is planned" —
is exactly why this does NOT live in ``validation.rules``/``validation.engine``:
every rule there has the shape ``Callable[[NormalizedItinerary,
SearchRequest], Rejection | None]`` (see ``validation/rules.py``), which
requires an itinerary that does not exist yet at plan time. There is
nothing to validate per-offer here, only a per-ORIGIN go/no-go decision
made from ``airports.registry`` data alone, before ``orchestration.plan``
ever builds a ``SearchTask`` for that origin.

``RejectionCode.GROUND_TRAVEL_EXCEEDED`` (domain/enums.py, built ahead of
schedule in Phase 1) is reused here for exactly the reason its own
docstring already gives — it is the closed-set reason code for "this
origin/itinerary is excluded", and this module is its plan-time producer.
``domain.validation.Rejection`` is likewise reused unchanged: an origin
exclusion is recorded in the exact same shape as an itinerary rejection
(code/message/observed/expected/rule_id), just keyed by IATA code in the
message instead of by ``itinerary_id`` (there is no itinerary yet).

All ten of today's configured origins (config/ground_access.yaml) are
inside the default 150-minute limit — this filter is therefore a no-op
against the current roster and is EXPECTED to stay that way. It must exist
and be correctly tested regardless, because master plan S6 says exactly
why: "it must exist because someone will add an eleventh airport."

Deliberately does NOT modify ``orchestration.plan`` (T37, already
committed) — this is a new, free-standing, directly-testable function that
reads ``airports.registry.origins()``'s ground data itself, so a future
caller (a later orchestration wiring task) can filter the origin list
BEFORE handing it to ``orchestration.plan.build_multi_origin_plan``/
``build_plan_for_origin``, without either of those two functions needing
to change.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from flightagent.airports.registry import Airport
from flightagent.airports.registry import origins as registry_origins
from flightagent.config.loader import load_config
from flightagent.config.models import FlightAgentSettings
from flightagent.domain.enums import RejectionCode
from flightagent.domain.validation import Rejection

_ONE_MINUTE = timedelta(minutes=1)


def _ground_duration_minutes(airport: Airport) -> int:
    """Exact integer minutes from ``airport.ground.duration`` — ``timedelta
    // timedelta`` is exact integer floor-division, never routed through
    ``float`` (finding 0.3's convention, reused here for consistency with
    every other duration-to-minutes conversion in this codebase).

    Raises ``ValueError`` if ``airport`` has no ``ground`` leg at all —
    calling this on a destination (which never carries one, see
    ``Airport.is_origin``) is a caller error, not a value this filter
    should silently treat as "0 minutes, always passes".
    """
    if airport.ground is None:
        raise ValueError(
            f"{airport.iata!r} has no ground-access leg — this filter only applies to "
            f"ORIGIN airports (see Airport.is_origin); a destination should never reach it"
        )
    return airport.ground.duration // _ONE_MINUTE


def filter_origins_by_ground_limit(
    origins: Sequence[Airport],
    *,
    max_ground_travel_minutes: int,
) -> tuple[list[Airport], list[Rejection]]:
    """D7's hard filter: split ``origins`` into (plannable, excluded-with-reason).

    An origin is excluded the instant its ``GroundLeg.duration`` STRICTLY
    EXCEEDS ``max_ground_travel_minutes`` — exactly at the limit (150
    minutes by default) still passes; master plan S6 says "exceeds", not
    "meets or exceeds". Every excluded origin gets exactly one
    ``Rejection`` with ``code=RejectionCode.GROUND_TRAVEL_EXCEEDED``,
    ``rule_id="ground_travel_limit"``, and the IATA code named in its
    message — there is no ``itinerary_id`` at this point, because this is a
    per-origin, not a per-itinerary, rejection.

    Order-preserving: the returned ``plannable`` list keeps ``origins``'s
    own relative order (priority order, when called with
    ``airports.registry.origins()``) — a caller depending on Addendum 2's
    priority ordering sees it undisturbed.
    """
    plannable: list[Airport] = []
    rejections: list[Rejection] = []
    for airport in origins:
        duration_minutes = _ground_duration_minutes(airport)
        if duration_minutes > max_ground_travel_minutes:
            rejections.append(
                Rejection(
                    code=RejectionCode.GROUND_TRAVEL_EXCEEDED,
                    message=(
                        f"{airport.iata}'s ground-access duration is {duration_minutes} "
                        f"minutes, exceeding the {max_ground_travel_minutes}-minute limit "
                        f"(D7, master plan S6) — excluded from planning entirely, before "
                        f"any task for this origin was built"
                    ),
                    observed=str(duration_minutes),
                    expected=f"<= {max_ground_travel_minutes}",
                    rule_id="ground_travel_limit",
                )
            )
        else:
            plannable.append(airport)
    return plannable, rejections


def plannable_origins(
    *,
    settings: FlightAgentSettings | None = None,
) -> tuple[list[Airport], list[Rejection]]:
    """The real, production entry point: ``airports.registry.origins()``
    filtered by ``settings.ground_travel.max_ground_travel_minutes``.

    ``settings`` defaults to ``load_config()`` when omitted, matching the
    exact convention ``orchestration.plan.build_plan_for_origin``/
    ``build_multi_origin_plan`` already use for the same reason: a test
    can pass an already-resolved (or deliberately overridden)
    ``FlightAgentSettings`` instead of touching the real config files or
    environment.
    """
    resolved_settings = settings if settings is not None else load_config()
    return filter_origins_by_ground_limit(
        registry_origins(),
        max_ground_travel_minutes=resolved_settings.ground_travel.max_ground_travel_minutes,
    )
