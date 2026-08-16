"""Score components and the final scored+ranked itinerary record.

Finding 0.3: ``Decimal`` throughout, never ``float`` — float addition is
not associative, so different accumulation orders under concurrent 160-way
fan-out would produce different last bits and reorder ties, which is a
reproducibility failure, not a cosmetic one.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from flightagent.domain.airport import IataCode
from flightagent.domain.ground import GroundLeg
from flightagent.domain.itinerary import NormalizedItinerary


class ScoreComponents(BaseModel):
    """Named-component vector — master plan finding 0.8: "emit the score as
    a named-component vector, never a scalar."

    ``score`` excludes the direct bonus per spec S5.6; ``adjusted_score``
    includes it per Addendum 1. Finding 0.1 is exactly why these must stay
    two different numbers rather than being unified: ``adjusted_score``
    governs ranked-list ordering, while the separate 150/20% ladder
    (policy.py) governs the per-destination narrative recommendation, and
    the two can legitimately disagree above roughly a EUR755 one-stop fare.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fare_eur: Decimal
    elapsed_time_component: Decimal
    layover_penalty: Decimal
    direct_bonus: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> Decimal:
        return self.fare_eur + self.elapsed_time_component + self.layover_penalty

    @computed_field  # type: ignore[prop-decorator]
    @property
    def adjusted_score(self) -> Decimal:
        return self.score + self.direct_bonus


class ScoredItinerary(BaseModel):
    """One itinerary with its score components, ground-cost overlay (D7),
    and rank positions.

    ``tiebreak_key`` is finding 0.3's fix: a deterministic sort key ending
    in ``itinerary_id`` so ties on ``adjusted_score``, price, and duration
    can never fall through to Python's stable sort and leak provider
    response arrival order into the reported ranking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    itinerary: NormalizedItinerary
    components: ScoreComponents
    ground: GroundLeg | None = None
    ground_cost_component: Decimal = Decimal(0)
    ground_time_component: Decimal = Decimal(0)
    rank_by_adjusted_score: int = Field(ge=1)
    rank_by_total_journey_score: int = Field(ge=1)
    rank_by_price: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_ground_consistency(self) -> Self:
        if self.ground is None and (
            self.ground_cost_component != 0 or self.ground_time_component != 0
        ):
            raise ValueError(
                "ground is None but a nonzero ground_cost_component/ground_time_component "
                "was supplied"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_journey_score(self) -> Decimal:
        """``adjusted_score + ground_cost_component + ground_time_component``
        (D7). Computed, never independently stored, so it cannot drift
        from its own definition."""
        return (
            self.components.adjusted_score + self.ground_cost_component + self.ground_time_component
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tiebreak_key(self) -> tuple[Decimal, Decimal, int, str]:
        """``(adjusted_score, price_eur, duration_seconds, itinerary_id)`` —
        D16's shipped ranking order, with ``itinerary_id`` last and
        non-optional (finding 0.3: "do not omit the itinerary_id term")."""
        return (
            self.components.adjusted_score,
            self.itinerary.price_eur.amount,
            int(self.itinerary.total_duration.total_seconds()),
            self.itinerary.itinerary_id,
        )


_ONE_MINUTE = timedelta(minutes=1)


class OriginSummary(BaseModel):
    """One origin's row for the Origin Comparison table (T40/T41 — master
    plan acceptance criterion A2-5c: "the origin comparison table lists a
    cheapest-valid price for all 10 origins ... or an explicit reason for
    absence").

    This is a per-ORIGIN reduction — one overall cheapest ``ScoredItinerary``
    for the origin, irrespective of which destination it goes to — not the
    per-DESTINATION-across-origins comparison D12's early-stop rule uses
    (T39, ``orchestration``), and not the 10x8 per-(origin, destination)
    fare matrix (``assets/fare-matrix.svg``, a separate, not-yet-built
    artifact). A2-5c asks for exactly one price per origin, which is what
    ``best`` is.

    ``best`` is ``None`` when this origin produced zero accepted
    itineraries in the run — the row still exists (see
    ``scoring.origin_summary.summarize_by_origin``), matching this
    codebase's "never silently discard a row" convention (``deduplicate``,
    ``rank_itineraries``'s destination-drop warning). Producing the
    human-readable REASON text for that absence (e.g. from the run's
    ``TaskOutcome``s) is a report-layer concern (T41), not this model's —
    this model only guarantees the row is present and machine-distinguishes
    "has a price" from "does not".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: IataCode
    best: ScoredItinerary | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ground_minutes(self) -> int | None:
        """Whole minutes from ``best.ground.duration``.

        ``None`` when ``best`` is ``None`` (no itinerary to read a ground
        leg off of) OR when ``best.ground`` is itself ``None`` (a scored
        itinerary that predates T38's ground-cost wiring never carries
        one). Computed, never independently stored — matching this class's
        sibling ``total_journey_score`` above — so it can never drift from
        what ``best`` actually carries.
        """
        if self.best is None or self.best.ground is None:
            return None
        return self.best.ground.duration // _ONE_MINUTE
