"""Direct-vs-stop policy output and early-stop audit record.

Phases 5/6 (out of scope here) build the rule engines that produce these;
this module only defines what they must produce.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from flightagent.domain.airport import IataCode
from flightagent.domain.enums import DirectTier
from flightagent.domain.itinerary import NormalizedItinerary
from flightagent.domain.money import Money


class DestinationAnalysis(BaseModel):
    """One destination's direct-vs-one-stop comparison (Addendum 1, D10).

    ``score_policy_divergence`` is finding 0.1's fix: the ``adjusted_score``
    ranking and this tier can disagree above roughly a EUR755 one-stop
    fare — the common case for July India fares — and this flag plus
    ``divergence_explanation`` is how the report says so in one sentence
    instead of silently contradicting itself two sections apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: IataCode
    cheapest_direct: NormalizedItinerary | None = None
    cheapest_valid_stop: NormalizedItinerary | None = None
    price_difference: Money | None = None
    relative_difference: Decimal | None = None
    tier: DirectTier
    tier_reason: str
    time_saved: timedelta | None = None
    score_policy_divergence: bool = False
    divergence_explanation: str | None = None

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        if self.score_policy_divergence and self.divergence_explanation is None:
            raise ValueError(
                "score_policy_divergence is True but divergence_explanation is missing"
            )
        if self.tier == DirectTier.NOT_AVAILABLE and self.cheapest_direct is not None:
            raise ValueError("tier is NOT_AVAILABLE but cheapest_direct is present")
        if self.tier != DirectTier.NOT_AVAILABLE and self.cheapest_direct is None:
            raise ValueError(f"tier is {self.tier} but cheapest_direct is missing")
        return self


class EarlyStopEvaluation(BaseModel):
    """Audit record for the EUR250 early-stop rule (D12, finding 0.7).

    ``compared_against`` is deliberately non-optional: master plan S4 —
    "an order-dependent rule with an implicit comparison set is
    unauditable" — so every evaluation must record exactly which origins
    it compared against, not just whether it triggered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at_wave: int
    triggered: bool
    triggering_origin: IataCode | None = None
    triggering_destination: IataCode | None = None
    margin: Money | None = None
    compared_against: tuple[IataCode, ...]
    mode: Literal["enforced", "advisory"]

    @model_validator(mode="after")
    def _validate_trigger_consistency(self) -> Self:
        fields_present = (
            self.triggering_origin is not None,
            self.triggering_destination is not None,
            self.margin is not None,
        )
        if self.triggered and not all(fields_present):
            raise ValueError(
                "triggered is True but triggering_origin/triggering_destination/margin is missing"
            )
        if not self.triggered and any(fields_present):
            raise ValueError(
                "triggered is False but a triggering_origin/triggering_destination/margin "
                "was supplied"
            )
        return self
