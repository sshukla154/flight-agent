"""normalize — turns a ``RawOffer`` into a fully-computed ``NormalizedItinerary``.

Master plan S3: this package may import ``domain`` and ``config`` only.

Phase 2 (T11) scope: ``builder.py`` (the itinerary builder + shape-key
hashing) and ``timezones.py`` (an IATA -> IANA lookup convenience). Two
siblings named in the master plan's module map are deliberately not built
yet and are out of scope for this task:

- ``fx.py`` — FX conversion. Not needed until a non-EUR provider exists
  (Phase 7); until then, ``builder.build_normalized_itinerary`` raises
  ``UnsupportedCurrencyError`` on a non-EUR price rather than converting.
- ``dedup.py`` — grouping normalized itineraries by ``shape_key`` and
  picking a survivor (Phase 3, T20). This package computes the shape key
  per itinerary; it does not group by it.
"""

from __future__ import annotations

from flightagent.normalize.builder import (
    NormalizationInvariantError,
    UnsupportedCurrencyError,
    build_normalized_itinerary,
    compute_shape_key,
)
from flightagent.normalize.timezones import iana_tz_for, zone_for

__all__ = [
    "NormalizationInvariantError",
    "UnsupportedCurrencyError",
    "build_normalized_itinerary",
    "compute_shape_key",
    "iana_tz_for",
    "zone_for",
]
