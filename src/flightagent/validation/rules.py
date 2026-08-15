"""Validation rules — one callable per rule (T12, v1 subset).

Master plan S1/S5 and DECISIONS.md's spec-defect table (finding 0.4):
"the validation engine re-checks stop count, layover, origin, and date on
every offer regardless" — every rule here is a pure, independent
predicate over ``(itinerary, request)``. A rule never knows whether a
sibling rule already failed, and never raises to signal a failure; it
returns a single ``Rejection`` or ``None``. ``engine.py`` owns running
every rule in ``RULES`` and collecting every non-``None`` result — that is
what "never short-circuits" actually means, and it is cheap to get right
here so Phase 3 T18 / Phase 6 T38 can append another callable to ``RULES``
without touching the engine.

v1 is a deliberate SUBSET: only stop count, the D8 layover window, origin
match, and the D11 origin-local departure date. Overnight/multi-day
layovers, DST edge cases beyond what ``Segment``/``Layover`` already
represent, self-transfer detection, and the ground-travel filter are out
of scope here (T18, T38).
"""

from __future__ import annotations

from collections.abc import Callable

from flightagent.domain.enums import RejectionCode
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.run import SearchRequest
from flightagent.domain.validation import Rejection

Rule = Callable[[NormalizedItinerary, SearchRequest], Rejection | None]


def check_stop_count(itinerary: NormalizedItinerary, request: SearchRequest) -> Rejection | None:
    """``RejectionCode.TOO_MANY_STOPS``: ``itinerary.stop_count`` must not
    exceed ``request.max_stops``.

    ``stop_count`` is an existing ``computed_field`` on
    ``NormalizedItinerary`` (domain/itinerary.py, Phase 1) — this rule
    reads it, it does not recompute connection counting itself.
    """
    if itinerary.stop_count > request.max_stops:
        return Rejection(
            code=RejectionCode.TOO_MANY_STOPS,
            message=(
                f"itinerary has {itinerary.stop_count} stop(s), exceeding the requested "
                f"maximum of {request.max_stops}"
            ),
            observed=str(itinerary.stop_count),
            expected=f"<= {request.max_stops}",
            rule_id="stop_count",
        )
    return None


def check_layover_window(
    itinerary: NormalizedItinerary, request: SearchRequest
) -> Rejection | None:
    """``RejectionCode.LAYOVER_TOO_SHORT`` / ``.LAYOVER_TOO_LONG``: D8's
    closed ``[request.layover_min, request.layover_max]`` window
    (``[180, 360]`` minutes by default), checked against
    ``Layover.duration`` — which ``domain/segment.py`` already computes
    exclusively on UTC-elapsed time (never on local wall-clock
    subtraction, which breaks across a DST boundary).

    D13: a DIRECT itinerary (``stop_count == 0``) has no layover to
    validate and this rule does not apply to it AT ALL — Addendum 1
    scopes the 3-6h rule to one-stop itineraries only. This is checked
    explicitly up front, not left to the accident of a direct itinerary's
    ``layovers`` tuple always being empty (which it also is, by
    ``Leg._validate_shape``'s own invariant): D13 is a rule-applicability
    decision, and it belongs in this function's own logic, not folklore
    about what ``Leg`` happens to enforce elsewhere.
    """
    if itinerary.stop_count == 0:
        return None

    for leg in itinerary.legs:
        for layover in leg.layovers:
            if layover.duration < request.layover_min:
                return Rejection(
                    code=RejectionCode.LAYOVER_TOO_SHORT,
                    message=(
                        f"layover at {layover.airport} is {layover.duration}, shorter than "
                        f"the {request.layover_min} minimum (D8)"
                    ),
                    observed=str(layover.duration),
                    expected=f">= {request.layover_min}",
                    rule_id="layover_window",
                )
            if layover.duration > request.layover_max:
                return Rejection(
                    code=RejectionCode.LAYOVER_TOO_LONG,
                    message=(
                        f"layover at {layover.airport} is {layover.duration}, longer than "
                        f"the {request.layover_max} maximum (D8)"
                    ),
                    observed=str(layover.duration),
                    expected=f"<= {request.layover_max}",
                    rule_id="layover_window",
                )
    return None


def check_origin_match(itinerary: NormalizedItinerary, request: SearchRequest) -> Rejection | None:
    """``RejectionCode.ORIGIN_MISMATCH``: the itinerary's actual departure
    airport (the first segment of its first leg) must equal
    ``request.origin``.

    "Case-insensitive on the IATA code" per the Phase 1 test convention
    (``test_domain_smoke.py::TestIataCode::test_iata_code_rejects_lowercase``,
    ``test_airports.py::test_airport_model_direct_import_uses_domain_iata_code``):
    ``IataCode`` (domain/airport.py) does not fold case at all — it
    REJECTS anything but ``^[A-Z]{3}$`` at construction time. Any
    ``IataCode`` value that has survived into a validated ``Segment`` or
    ``SearchRequest`` is therefore already uppercase by construction, so
    there is no case difference left for this rule to fold. Calling
    ``.upper()``/``.lower()`` here would be dead defensive code papering
    over a guarantee the type layer already gives for free — this is
    exactly the "check first" case, not a "reimplement it" one.
    """
    actual_origin = itinerary.legs[0].segments[0].origin
    if actual_origin != request.origin:
        return Rejection(
            code=RejectionCode.ORIGIN_MISMATCH,
            message=(
                f"itinerary departs from {actual_origin}, not the requested origin "
                f"{request.origin}"
            ),
            observed=actual_origin,
            expected=request.origin,
            rule_id="origin_match",
        )
    return None


def check_departure_date(
    itinerary: NormalizedItinerary, request: SearchRequest
) -> Rejection | None:
    """``RejectionCode.DATE_MISMATCH``: D11 — the departure date is
    evaluated in the ORIGIN's LOCAL date, never UTC.

    ``Segment.depart_local`` (domain/segment.py) already carries the
    origin-local wall-clock reading, mutually validated against
    ``depart_utc``/``origin_tz``/``depart_fold`` at construction time —
    reading ``.date()`` off it IS the origin-local comparison D11 asks
    for. This rule must NOT derive the date from ``depart_utc``: a
    segment departing 2027-07-18 00:30 CEST is 2027-07-17 22:30 UTC, and
    a UTC-date comparison would wrongly accept it against a
    2027-07-17 request.
    """
    first_segment = itinerary.legs[0].segments[0]
    local_departure_date = first_segment.depart_local.date()
    if local_departure_date != request.departure_date:
        return Rejection(
            code=RejectionCode.DATE_MISMATCH,
            message=(
                f"itinerary's origin-local departure date is {local_departure_date}, not "
                f"the requested {request.departure_date} (D11: evaluated in origin-local "
                f"time, not UTC)"
            ),
            observed=str(local_departure_date),
            expected=str(request.departure_date),
            rule_id="departure_date_origin_local",
        )
    return None


RULES: tuple[Rule, ...] = (
    check_stop_count,
    check_layover_window,
    check_origin_match,
    check_departure_date,
)
"""Every rule this v1 engine runs. Append here, never branch on rule
identity elsewhere — this is the one list ``engine.py`` iterates, and the
list T18/T38 extend later."""
