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

from datetime import timedelta

from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.segment import Segment

_ONE_MINUTE = timedelta(minutes=1)


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
