"""flightagent.airports — the merged airport registry (Phase 1 / T8).

Joins ``config/airports.yaml`` (general reference data) with
``config/ground_access.yaml`` (Nieuwegein-specific ground access), per
master plan §3/§6. See ``registry.py`` for the full rationale, including
why ``Airport`` is not defined in ``flightagent.domain.airport`` even
though it belongs conceptually with the domain models.

The literal Phase 1 exit criterion (``len(origins()) == 10 and
len(destinations()) == 8``) is exercised against this package's public
surface, not against ``registry`` internals — see
``tests/unit/test_airports.py``.
"""

from __future__ import annotations

from flightagent.airports.registry import (
    Airport,
    AirportRegistry,
    AirportRegistryError,
    UnknownAirportError,
    destinations,
    get,
    origins,
    reload,
)

__all__ = [
    "Airport",
    "AirportRegistry",
    "AirportRegistryError",
    "UnknownAirportError",
    "destinations",
    "get",
    "origins",
    "reload",
]
