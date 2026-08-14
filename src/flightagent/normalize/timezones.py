"""IATA -> IANA timezone resolution, for any future caller that needs it.

This is deliberately a thin lookup convenience, not a second source of
timezone truth. ``flightagent.airports.registry`` (Phase 1 / T8) already
merges ``config/airports.yaml`` into one validated ``{iata: Airport}`` map
with an ``iana_tz`` field per airport, and already raises
``UnknownAirportError`` for an unrecognised code rather than returning
``None`` or defaulting to UTC (master plan S4: "The normalizer must fail
the task on an unknown IATA code, never default to UTC"). This module
reuses that lookup unchanged.

What this module deliberately does NOT do: DST/fold-aware arithmetic. That
already lives correctly on ``Segment``/``Layover`` (domain/segment.py,
Phase 1 / T7) — ``classify_local_time``, ``depart_fold``/``arrive_fold``,
and the UTC-exclusive duration computations were built and adversarially
audited there (spikes/tz_arithmetic.py). Reimplementing any of that here
would create a second, divergent copy of logic that is already correct.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from flightagent.airports import registry
from flightagent.domain.airport import IataCode


def iana_tz_for(iata: IataCode) -> str:
    """The IANA zone key for one airport, e.g. ``"AMS" -> "Europe/Amsterdam"``.

    Raises ``registry.UnknownAirportError`` for an unrecognised code —
    never returns ``None`` or a default zone.
    """
    return registry.get(iata).iana_tz


def zone_for(iata: IataCode) -> ZoneInfo:
    """The resolved ``ZoneInfo`` for one airport.

    Raises ``registry.UnknownAirportError`` for an unrecognised code (via
    ``iana_tz_for``), and ``zoneinfo.ZoneInfoNotFoundError`` in the
    (currently unreachable, since the catalog is committed data) case
    where the catalog names a zone key ``tzdata`` cannot resolve.
    """
    return ZoneInfo(iana_tz_for(iata))
