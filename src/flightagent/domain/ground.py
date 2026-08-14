"""Ground-access leg from a home location to a candidate origin airport.

Master plan S6: the spec's stated motive for searching 10 airports is
minimizing total travel cost, but its scoring formula ignores ground
travel entirely. ``GroundLeg`` is what closes that gap without touching
the spec's ``score``/``adjusted_score`` (D7) — it feeds a hard 2.5h
validation filter and a parallel ``total_journey_score`` overlay
(scoring.py), never the spec's own formula.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from flightagent.domain.airport import IataCode
from flightagent.domain.money import Money


class GroundLeg(BaseModel):
    """One origin's ground-access row from ``config/ground_access.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_location: str
    to_airport: IataCode
    mode: str
    """Free-form, config-driven (e.g. "car", "train", "bus") rather than a
    closed enum — the set of usable ground modes is a config/catalog
    concern, not something the domain model should hardcode and get out
    of sync with ``config/ground_access.yaml``."""
    duration: timedelta = Field(ge=timedelta(0))
    distance_km: Decimal = Field(ge=Decimal(0))
    cost: Money
    source: Literal["estimate", "measured", "user_supplied"]
    """Master plan S6: minutes are spec-sourced (Addendum 2); costs are
    estimates. Every row must say which, and it is surfaced in a report
    footnote."""
    as_of: date
    notes: str | None = None
