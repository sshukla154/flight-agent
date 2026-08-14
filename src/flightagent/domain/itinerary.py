"""Leg / offer / normalized-itinerary models.

Master plan S4 names ``Leg``, ``RawOffer`` and ``NormalizedItinerary`` but
only gives a full code block for ``Segment``. The shapes below follow
spikes/mapping_sketch.md's proposal (S1.2-1.4) and close the gaps that
document's S3 found: fare brands breaking dedup (S3.1/3.4), seat
availability being asymmetric across providers (S3.5), and refundability
being genuinely tri-state on Duffel (S3.6).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    model_validator,
)

from flightagent.domain.airport import CarrierCode
from flightagent.domain.money import Money
from flightagent.domain.segment import Layover, Segment


class Leg(BaseModel):
    """One directional journey: a chain of connecting segments.

    D2 keeps the itinerary's legs as a sequence from day one, so adding a
    return leg later is additive rather than a redesign.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    segments: tuple[Segment, ...] = Field(min_length=1)
    layovers: tuple[Layover, ...] = Field(default=())

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if len(self.layovers) != len(self.segments) - 1:
            raise ValueError(
                f"expected {len(self.segments) - 1} layovers for {len(self.segments)} segments, "
                f"got {len(self.layovers)}"
            )

        # Consecutive-pair iteration: segments[1:] is intentionally one shorter
        # than segments, so `strict=True` would be wrong here, not safer.
        for prev, nxt in zip(self.segments, self.segments[1:]):  # noqa: B905
            if prev.destination != nxt.origin:
                raise ValueError(f"segments do not connect: {prev.destination} != {nxt.origin}")

        for prev, layover, nxt in zip(self.segments, self.layovers, self.segments[1:]):  # noqa: B905
            if layover.arrive_utc != prev.arrive_utc:
                raise ValueError(
                    f"layover.arrive_utc {layover.arrive_utc.isoformat()} != preceding "
                    f"segment's arrive_utc {prev.arrive_utc.isoformat()}"
                )
            if layover.depart_utc != nxt.depart_utc:
                raise ValueError(
                    f"layover.depart_utc {layover.depart_utc.isoformat()} != following "
                    f"segment's depart_utc {nxt.depart_utc.isoformat()}"
                )

        # Master plan S4: derive the total from endpoints, then ASSERT the
        # sum-of-parts matches, rather than computing it twice and hoping.
        endpoints_total = self.segments[-1].arrive_utc - self.segments[0].depart_utc
        summed_total = sum((s.duration for s in self.segments), timedelta()) + sum(
            (layover.duration for layover in self.layovers), timedelta()
        )
        if endpoints_total != summed_total:
            raise ValueError(
                f"total_duration invariant violated: endpoints give {endpoints_total}, sum of "
                f"segments+layovers gives {summed_total}"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_duration(self) -> timedelta:
        """``last.arrive_utc - first.depart_utc``. Never derived by summing
        parts — ``_validate_shape`` asserts the sum matches this instead."""
        return self.segments[-1].arrive_utc - self.segments[0].depart_utc

    @computed_field  # type: ignore[prop-decorator]
    @property
    def connection_count(self) -> int:
        return len(self.segments) - 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def technical_stop_count(self) -> int:
        return sum(segment.technical_stops for segment in self.segments)


class RawOffer(BaseModel):
    """One offer exactly as quoted by a provider, pre-dedup.

    Proposed shape per spikes/mapping_sketch.md S1.3 — master plan S4
    names this model but does not spell it out.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    provider_offer_id: str
    legs: tuple[Leg, ...] = Field(min_length=1)
    price: Money
    offer_expires_at: AwareDatetime | None = None
    raw_payload_ref: str
    provider_booking_url: HttpUrl | None = None


class CodeshareReference(BaseModel):
    """One codeshare sibling collapsed into a dedup survivor by the shared
    itinerary shape key (finding 0.2) — carried for display ("also sold as
    AF 1234"), never for identity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    marketing_carrier: CarrierCode
    flight_number: str | None = None


class FareOption(BaseModel):
    """One collapsed offer's fare-brand detail, carried on the dedup
    survivor.

    mapping_sketch.md S3.1: the shape-key dedup key (finding 0.2) correctly
    collapses codeshares, but it also collapses distinct fare brands
    (Economy Saver vs Economy Flex at identical times) into one survivor.
    This is how that known, documented loss stays visible instead of
    silently keeping the cheapest and discarding the rest.

    Baggage and refundability are tri-state, not bool, because a provider
    genuinely not saying (Duffel's ``null``) is a different fact from a
    confirmed zero (mapping_sketch.md S3.2, S3.6) — collapsing "unknown"
    into "not included"/"not allowed" invents a restriction that may not
    exist.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: Money
    fare_brand: str | None = None
    checked_baggage: Literal["included", "not_included", "unknown"] = "unknown"
    refundable: Literal["allowed", "not_allowed", "unknown"] = "unknown"
    provider_offer_id: str | None = None


class NormalizedItinerary(BaseModel):
    """Post-normalization, post-dedup, provider-agnostic itinerary.

    "Everything from RawOffer" (mapping_sketch.md S1.4) plus the D14 FX
    fields, finding 0.2's dedup metadata, finding 0.6's booking-url kind,
    and the two additional gaps this task's brief calls out explicitly:
    ``fare_options`` (S3.1/3.4) and ``bookable_seats_remaining`` (S3.5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    itinerary_id: str
    provider: str
    legs: tuple[Leg, ...] = Field(min_length=1)
    price_original: Money
    price_eur: Money
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    fx_as_of: AwareDatetime | None = None
    booking_url: HttpUrl | None = None
    booking_url_kind: Literal["provider_native", "search_deeplink", "unavailable"]
    offer_expires_at: AwareDatetime | None = None
    shape_key: str
    duplicate_count: int = Field(default=1, ge=1)
    also_offered_by: tuple[CodeshareReference, ...] = ()
    fare_as_of: AwareDatetime
    fare_options: tuple[FareOption, ...] = ()
    bookable_seats_remaining: int | None = Field(default=None, ge=0)
    """Amadeus provides this (``numberOfBookableSeats``), Duffel does not
    (mapping_sketch.md S3.5) — nullable, never defaulted to a number: the
    report must be able to distinguish "2 seats left" from "we don't
    know", never render the absence as implied plenty."""

    @model_validator(mode="after")
    def _validate_fx(self) -> Self:
        fx_fields = (self.fx_rate, self.fx_source, self.fx_as_of)
        present = [field is not None for field in fx_fields]
        if any(present) and not all(present):
            raise ValueError(
                "fx_rate, fx_source and fx_as_of must be all-present or all-absent — D14 "
                "forbids a partially-disclosed conversion"
            )
        if self.price_original.currency != "EUR" and self.fx_rate is None:
            raise ValueError(
                f"price_original is {self.price_original.currency}, not EUR, but no fx_rate "
                f"was recorded — D14 forbids a silent conversion; reject the itinerary instead "
                f"of guessing a rate"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_duration(self) -> timedelta:
        return sum((leg.total_duration for leg in self.legs), timedelta())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stop_count(self) -> int:
        return sum(leg.connection_count for leg in self.legs)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def technical_stop_count(self) -> int:
        return sum(leg.technical_stop_count for leg in self.legs)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def marketing_carriers(self) -> frozenset[CarrierCode]:
        return frozenset(segment.marketing_carrier for leg in self.legs for segment in leg.segments)
