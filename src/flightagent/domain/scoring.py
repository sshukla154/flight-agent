"""Score components and the final scored+ranked itinerary record.

Finding 0.3: ``Decimal`` throughout, never ``float`` — float addition is
not associative, so different accumulation orders under concurrent 160-way
fan-out would produce different last bits and reorder ties, which is a
reproducibility failure, not a cosmetic one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

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
