"""``airport_info`` tool — spec §3.2: "Returns airport city and country".

Implemented as a superset of the spec's stated minimum: it returns the
full merged ``Airport`` record (which includes ``city`` and ``country``
among other fields — name, IANA tz, coordinates, and, for origins,
ground-access data) rather than a narrower ad hoc return shape, because
the richer record is what the rest of this project actually needs and
returning it still satisfies the letter of the spec (T8 task brief).
"""

from __future__ import annotations

from flightagent.airports import registry
from flightagent.airports.registry import Airport
from flightagent.domain.airport import IataCode


def airport_info(iata: IataCode) -> Airport:
    """Look up one airport's full merged record by IATA code.

    Raises ``registry.UnknownAirportError`` for an unrecognised code —
    never returns ``None`` or a default record. Master plan §4: a
    missing or wrong airport entry must fail loudly, because it would
    otherwise silently corrupt every duration computed through that
    airport downstream.
    """
    return registry.get(iata)
