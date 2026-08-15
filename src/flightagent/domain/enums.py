"""Closed enumerations for the flight-agent domain.

Master plan S4 names the model set; several members map directly onto a
specific finding or decision so a reader can trace an enum member back to
the reasoning that required it (RejectionCode -> tz spike + finding 0.2;
TaskState -> "8 terminal states"; RunStatus -> finding 0.5; DirectTier ->
D10).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class CabinClass(StrEnum):
    """Booking cabin. Economy at minimum (D3 scopes this project to economy
    only); the other three are included because mapping_sketch.md S2.2
    notes both providers can return a genuinely mixed-cabin itinerary, and
    a mapper that can only spell "economy" has nowhere to put that fact.
    """

    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


type StopMode = Literal[0, 1]
"""0 = direct-only, 1 = at most one stop.

D13's literal-int convention. Named here for readability, but deliberately
a type alias rather than an ``IntEnum``: every consumer of ``max_stops``
in this project (starting with ``SearchRequest.max_stops`` in run.py)
compares against the bare literal ints 0 and 1, and an ``IntEnum`` member
would still satisfy that by value but would be a second, separate way to
spell the same thing — this alias keeps there being exactly one.
"""


class RejectionCode(StrEnum):
    """Closed set of reasons an itinerary can be excluded from the valid
    ranked set.

    The engine that produces these (Phase 3, out of scope here) never
    short-circuits on the first failing rule — master plan S1: "the
    validation engine re-checks stop count, layover, origin, and date on
    every offer regardless" — so more than one code can legitimately apply
    to a single itinerary. That is exactly why ``validation.ValidationResult``
    carries a list of ``Rejection``, never a single optional one.

    MISSING_TIMEZONE / AMBIGUOUS_LOCAL_TIME / NONEXISTENT_LOCAL_TIME exist
    because of spikes/tz_arithmetic.py's finding: a Segment as originally
    specified cannot represent a DST fall-back (ambiguous) or
    spring-forward (nonexistent) local wall-clock reading, and this is not
    a rare edge case at 10 European origins.

    DESTINATION_MISMATCH (Phase 3, T18) is ORIGIN_MISMATCH's missing
    counterpart: the same "is this the airport the traveller actually
    asked for" check, applied to the arrival end instead of the departure
    end.

    SELF_TRANSFER (Phase 3, T18, D5) marks a self-transfer /
    separate-ticket connection: D5 excludes these from the valid ranked
    set entirely, because nothing protects the traveller if the first
    ticket runs late, however generous the layover looks on paper.
    """

    TOO_MANY_STOPS = "too_many_stops"
    LAYOVER_TOO_SHORT = "layover_too_short"
    LAYOVER_TOO_LONG = "layover_too_long"
    ORIGIN_MISMATCH = "origin_mismatch"
    DESTINATION_MISMATCH = "destination_mismatch"
    DATE_MISMATCH = "date_mismatch"
    CABIN_MISMATCH = "cabin_mismatch"
    GROUND_TRAVEL_EXCEEDED = "ground_travel_exceeded"
    MISSING_TIMEZONE = "missing_timezone"
    AMBIGUOUS_LOCAL_TIME = "ambiguous_local_time"
    NONEXISTENT_LOCAL_TIME = "nonexistent_local_time"
    SELF_TRANSFER = "self_transfer"
    INVARIANT_VIOLATION = "invariant_violation"


class TaskState(StrEnum):
    """The 8 terminal states one ``SearchTask`` can end in (master plan S4).

    Matches the fare-matrix cell states already designed and shown to the
    project owner (D17/D18): OK/NO_OFFERS/ALL_REJECTED/PROVIDER_ERROR map
    onto the matrix's priced/NO_OFFERS/ALL_REJECTED/PROVIDER_ERROR cells.
    """

    OK = "ok"
    NO_OFFERS = "no_offers"
    ALL_REJECTED = "all_rejected"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    SKIPPED_EARLY_STOP = "skipped_early_stop"
    CANCELLED = "cancelled"


class DirectTier(StrEnum):
    """D10's four-state band table for the direct-vs-one-stop narrative tier."""

    RECOMMENDED = "recommended"
    GOOD_VALUE = "good_value"
    NOT_RECOMMENDED = "not_recommended"
    NOT_AVAILABLE = "not_available"


class RunStatus(StrEnum):
    """Finding 0.5's four-way replacement for the spec's single hardcoded
    ``no_results`` string, which conflated "nothing matched the layover
    rule" with "the provider was down" — two very different situations
    that call for different user reactions.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_RESULTS = "no_results"
    FAILED = "failed"
