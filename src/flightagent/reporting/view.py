"""Shared derived-field helpers for the reporting v1 artifacts (T15).

Both ``reporting.markdown`` and ``reporting.json_report`` need the same
handful of values derived from a ``NormalizedItinerary`` -- the airline
string, the "AMS to DXB to DEL" route string, the total layover in
minutes, the formatted price. Factored out here, once, so the two
artifacts can never quietly disagree about what "the route" or "the
layover" of a given itinerary is -- a divergence there would be exactly
the kind of report-vs-JSON mismatch a reviewer would have to diff two
files to catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from flightagent.domain.enums import DirectTier, RejectionCode
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.run import _ERROR_STATES as _TASK_ERROR_STATES
from flightagent.domain.run import TaskOutcome
from flightagent.domain.segment import Segment

_ONE_MINUTE = timedelta(minutes=1)

_DIRECT_TIER_RECOMMENDATION_LABELS: dict[DirectTier, str] = {
    DirectTier.RECOMMENDED: "Recommended",
    DirectTier.GOOD_VALUE: "Recommended (good value)",
    DirectTier.NOT_RECOMMENDED: "Optional",
    DirectTier.NOT_AVAILABLE: "Not available",
}
"""Addendum 1's exact Recommendation-column text, per D10 tier (T33). The
``NOT_AVAILABLE`` -> ``"Not available"`` mapping is an explicit acceptance
criterion (at least one destination must show this exact string)."""


def first_segment(itinerary: NormalizedItinerary) -> Segment:
    """The itinerary's very first segment -- its departure point."""
    return itinerary.legs[0].segments[0]


def last_segment(itinerary: NormalizedItinerary) -> Segment:
    """The itinerary's very last segment -- its arrival point."""
    return itinerary.legs[-1].segments[-1]


def route_string(itinerary: NormalizedItinerary) -> str:
    """``"AMS to DXB to DEL"`` -- spec section 6's own example format,
    matched literally (airport codes joined by the word " to ", never an
    arrow or dash).

    D2 keeps ``legs`` as a sequence for a future return trip; if more than
    one leg is ever present, each leg's own route string is joined with
    ``" / "`` -- there is no single-leg codepath to special-case away.
    """
    leg_routes = []
    for leg in itinerary.legs:
        airports = [leg.segments[0].origin, *(segment.destination for segment in leg.segments)]
        leg_routes.append(" to ".join(airports))
    return " / ".join(leg_routes)


def airline_string(itinerary: NormalizedItinerary) -> str:
    """Marketing carrier code(s), sorted and joined with ``"/"``.

    No airline-name lookup table exists anywhere in this codebase yet
    (only IATA carrier codes, ``domain.airport.CarrierCode``) -- rendering
    the code(s) is the honest v1 answer rather than inventing a name.
    """
    return "/".join(sorted(itinerary.marketing_carriers))


def total_layover_minutes(itinerary: NormalizedItinerary) -> int:
    """Sum of every layover's duration across every leg, in whole minutes.

    Zero layovers (a direct itinerary) sums to ``0`` with no special
    casing. ``timedelta // timedelta`` is exact integer floor-division --
    never routes through ``float`` (finding 0.3's convention, reused here
    for display purposes too, for consistency with ``scoring.score``).
    """
    total = timedelta()
    for leg in itinerary.legs:
        for layover in leg.layovers:
            total += layover.duration
    return total // _ONE_MINUTE


def total_duration_minutes(itinerary: NormalizedItinerary) -> int:
    """The itinerary's total elapsed duration, in whole minutes."""
    return itinerary.total_duration // _ONE_MINUTE


def format_layover(total_minutes: int) -> str:
    """``"4h 10m"`` for a one-stop itinerary; ``"Direct (no layover)"``
    for a direct one (``total_minutes == 0``)."""
    if total_minutes == 0:
        return "Direct (no layover)"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def format_price_eur(price: Money) -> str:
    """``"€725.00"``. ``Money.amount`` is already quantized to the
    cent (ROUND_HALF_UP) at construction time, so ``:.2f`` here only
    displays the two decimal places already present -- it never re-rounds
    a value that wasn't already exactly two decimal digits.
    """
    return f"€{price.amount:.2f}"


def destination_from_task_id(task_id: str) -> str:
    """The destination IATA code embedded in a ``task_id``.

    ``domain.ids.compute_task_id`` builds every ``task_id`` as
    ``f"{origin}-{destination}-s{max_stops}"`` (master plan S7). IATA codes
    are always exactly three uppercase letters (``domain.airport.IataCode``)
    and never contain a hyphen, so splitting on ``"-"`` and taking the
    middle field is an exact inverse of that construction, not a
    best-effort guess.
    """
    parts = task_id.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"task_id {task_id!r} does not match the "
            "'{origin}-{destination}-s{max_stops}' shape produced by "
            "domain.ids.compute_task_id"
        )
    return parts[1]


def origin_from_task_id(task_id: str) -> str:
    """The origin IATA code embedded in a ``task_id`` -- ``destination_from_task_id``'s
    mirror image, reading the FIRST field of the same ``"{origin}-{destination}-s{max_stops}"``
    shape instead of the middle one (T41's Origin Comparison table needs to
    scope a run's ``TaskOutcome``s down to one origin's own tasks; nothing
    before it needed the inverse this function provides)."""
    parts = task_id.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"task_id {task_id!r} does not match the "
            "'{origin}-{destination}-s{max_stops}' shape produced by "
            "domain.ids.compute_task_id"
        )
    return parts[0]


def direct_tier_recommendation_label(tier: DirectTier) -> str:
    """Addendum 1's exact Recommendation-column text for one ``DirectTier``
    (D10, T33) -- shared by ``reporting.markdown`` (which additionally
    prefixes a star marker for ``RECOMMENDED``, a display-only decoration)
    and ``reporting.json_report`` (which emits this text verbatim), so the
    two artifacts' Recommendation values can never quietly drift apart the
    way two independently-maintained string literals eventually would.
    """
    return _DIRECT_TIER_RECOMMENDATION_LABELS[tier]


def failed_task_outcomes(task_outcomes: Sequence[TaskOutcome]) -> list[TaskOutcome]:
    """The subset of ``task_outcomes`` in an error state (``domain.run``'s
    ``_ERROR_STATES``: PROVIDER_ERROR, RATE_LIMITED, TIMEOUT), in the same
    relative order as given.

    Factored out here for the same reason as every other helper in this
    module: the Markdown and JSON renderers must never quietly disagree
    about which tasks count as "failed" for the "Failed Searches" section
    (Phase 4, T26).
    """
    return [outcome for outcome in task_outcomes if outcome.state in _TASK_ERROR_STATES]


def self_transfer_airports(itinerary: NormalizedItinerary) -> list[str]:
    """IATA code(s) of every layover in ``itinerary`` flagged
    ``Layover.is_self_transfer`` (D5, T18) -- computed directly from the
    itinerary's own layovers, in leg/segment order, rather than parsed out
    of a ``Rejection``'s free-form ``message`` string, so the Self-Transfer
    appendix (T41) never depends on that message's exact wording staying
    stable. Usually a single entry (one self-transfer connection), but D2's
    multi-leg ``legs`` shape leaves room for more than one.
    """
    return [
        layover.airport
        for leg in itinerary.legs
        for layover in leg.layovers
        if layover.is_self_transfer
    ]


def origin_absence_reason(origin: str, task_outcomes: Sequence[TaskOutcome]) -> str:
    """Human-readable reason an ``OriginSummary.best`` is ``None`` for
    ``origin`` (T41, master plan acceptance criterion A2-5c: "... or an
    explicit reason for absence") -- scoped to the subset of
    ``task_outcomes`` whose ``task_id`` names this origin
    (``origin_from_task_id``), mirroring ``cli._dominant_rejection_code``'s
    own combine-then-pick-the-max logic but at one origin's scope instead
    of the whole run's.

    Ordered checks, most specific first: an origin with no task in the
    ledger at all was never searched this run; an origin with >=1
    error-state task failed outright, before any rejection reasoning is
    even meaningful; otherwise the origin's most frequent ``RejectionCode``
    (combined across every one of its tasks) is named; and an origin with
    tasks but zero offers and zero rejections simply returned nothing.
    """
    origin_outcomes = [
        outcome for outcome in task_outcomes if origin_from_task_id(outcome.task_id) == origin
    ]
    if not origin_outcomes:
        return "origin not searched in this run"
    if any(outcome.state in _TASK_ERROR_STATES for outcome in origin_outcomes):
        return "search failed for this origin (provider error)"

    totals: dict[RejectionCode, int] = {}
    for outcome in origin_outcomes:
        for code, count in outcome.rejection_counts.items():
            totals[code] = totals.get(code, 0) + count
    if totals:
        dominant = max(totals.items(), key=lambda item: (item[1], item[0].value))[0]
        return f"no valid itineraries (dominant rejection: {dominant.value})"
    return "no offers returned for this origin"
