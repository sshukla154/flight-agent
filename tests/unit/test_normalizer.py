"""Tests for ``flightagent.normalize`` (Phase 2 / T11).

Master plan S4's biggest finding for this module, restated here because
it shapes what these tests actually exercise: ``total_duration``,
``stop_count`` and ``technical_stop_count`` are already ``computed_field``
properties on ``Leg``/``NormalizedItinerary`` (domain/itinerary.py,
Phase 1 / T7), and the endpoints-vs-sum invariant is already enforced by
``Leg._validate_shape`` at construction time. So
``TestInconsistentInvariantRaises`` below first proves that fact (a
normally-constructed inconsistent ``Leg`` is rejected by the domain layer
itself, before ``build_normalized_itinerary`` ever runs), then uses
``model_construct`` to bypass that domain-level guard and exercise this
module's own defense-in-depth re-assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.segment import Layover, Segment
from flightagent.normalize.builder import (
    NormalizationInvariantError,
    UnsupportedCurrencyError,
    build_normalized_itinerary,
)

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
DUBAI = ZoneInfo("Asia/Dubai")
KOLKATA = ZoneInfo("Asia/Kolkata")

_FARE_AS_OF = datetime(2027, 7, 1, 12, 0, tzinfo=UTC)


def _ams_dxb_segment() -> Segment:
    """AMS -> DXB, 08:00 duration, no technical stop."""
    depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
    arrive_utc = datetime(2027, 7, 17, 18, 0, tzinfo=UTC)
    return Segment(
        segment_id="AMS-DXB-147",
        origin="AMS",
        destination="DXB",
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(AMSTERDAM),
        arrive_local=arrive_utc.astimezone(DUBAI),
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
        marketing_carrier="EK",
        flight_number="147",
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _dxb_del_segment(
    *, depart_utc: datetime | None = None, technical_stops: int = 1
) -> Segment:
    """DXB -> DEL, 02:30 duration, ONE technical stop by default — set
    deliberately != 0 so a test that only checked ``stop_count`` couldn't
    accidentally pass by reading ``technical_stop_count``'s value instead,
    or vice versa; the two fields must independently be right.
    """
    depart_utc = depart_utc if depart_utc is not None else datetime(2027, 7, 17, 21, 30, tzinfo=UTC)
    arrive_utc = depart_utc + timedelta(hours=2, minutes=30)
    return Segment(
        segment_id="DXB-DEL-232",
        origin="DXB",
        destination="DEL",
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(DUBAI),
        arrive_local=arrive_utc.astimezone(KOLKATA),
        origin_tz="Asia/Dubai",
        destination_tz="Asia/Kolkata",
        marketing_carrier="EK",
        flight_number="232",
        cabin=CabinClass.ECONOMY,
        technical_stops=technical_stops,
        duration=arrive_utc - depart_utc,
    )


def _dxb_layover(inbound: Segment, outbound: Segment) -> Layover:
    return Layover(
        airport="DXB",
        arrive_utc=inbound.arrive_utc,
        depart_utc=outbound.depart_utc,
        duration=outbound.depart_utc - inbound.arrive_utc,
        local_window=(inbound.arrive_local, outbound.depart_local),
        requires_airport_change=False,
        requires_terminal_change=False,
    )


def _ams_dxb_del_leg() -> Leg:
    """A consistent two-segment AMS -> DXB -> DEL leg.

    08:00 (AMS-DXB) + 03:30 layover (210min, within D8's [180,360]) +
    02:30 (DXB-DEL) = 14:00 total, endpoints-vs-sum invariant holds
    exactly (and is enforced by ``Leg._validate_shape`` at construction).
    """
    inbound = _ams_dxb_segment()
    outbound = _dxb_del_segment()
    layover = _dxb_layover(inbound, outbound)
    return Leg(segments=(inbound, outbound), layovers=(layover,))


def _raw_offer(
    leg: Leg,
    *,
    price: Money | None = None,
    provider_offer_id: str = "mock-offer-1",
    provider_booking_url: str | None = None,
) -> RawOffer:
    return RawOffer(
        provider="mock",
        provider_offer_id=provider_offer_id,
        legs=(leg,),
        price=price if price is not None else Money(amount=Decimal("650.00"), currency="EUR"),
        offer_expires_at=None,
        raw_payload_ref=f"mock:{provider_offer_id}",
        provider_booking_url=provider_booking_url,
    )


class TestDerivedFieldsFromTwoSegmentLeg:
    """AMS -> DXB -> DEL: total_duration/stop_count/technical_stop_count.

    All three assertions below read values that are ``computed_field``
    properties pre-existing on ``NormalizedItinerary`` (see module
    docstring) — this test exercises that ``build_normalized_itinerary``
    wires ``legs`` through correctly, not a recomputation this module
    performs independently.
    """

    def test_total_duration_stop_count_and_technical_stop_count(self) -> None:
        raw_offer = _raw_offer(_ams_dxb_del_leg())

        itinerary = build_normalized_itinerary(
            raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        assert itinerary.total_duration == timedelta(hours=14)
        assert itinerary.stop_count == 1  # one connection: AMS-DXB-DEL
        assert itinerary.technical_stop_count == 1  # from the DXB-DEL segment only

    def test_price_eur_equals_price_original_when_already_eur(self) -> None:
        price = Money(amount=Decimal("650.00"), currency="EUR")
        raw_offer = _raw_offer(_ams_dxb_del_leg(), price=price)

        itinerary = build_normalized_itinerary(
            raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        assert itinerary.price_eur == price
        assert itinerary.price_original == price
        assert itinerary.fx_rate is None

    def test_no_booking_url_maps_to_unavailable_kind(self) -> None:
        raw_offer = _raw_offer(_ams_dxb_del_leg(), provider_booking_url=None)

        itinerary = build_normalized_itinerary(
            raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        assert itinerary.booking_url_kind == "unavailable"
        assert itinerary.booking_url is None


class TestInconsistentInvariantRaises:
    """Confirms the "check first" instruction's finding, then confirms
    this module's own defense-in-depth assertion on top of it.
    """

    def test_leg_itself_already_rejects_a_mismatched_duration(self) -> None:
        """The domain model's OWN validator (``Leg._validate_shape``)
        already prevents this at construction time — no dependency on
        this module. Documented per the task brief's "check first, they
        may [already prevent this]" instruction.
        """
        inbound = _ams_dxb_segment()
        # Lie about the second segment's own duration field (true gap is
        # 02:30; claim 03:30) while keeping depart_utc/arrive_utc/origin/
        # destination all correctly connected — this isolates the
        # invariant check from the connectivity checks.
        lying_outbound = Segment.model_construct(
            **{
                **_dxb_del_segment().__dict__,
                "duration": timedelta(hours=3, minutes=30),
            }
        )
        layover = _dxb_layover(inbound, lying_outbound)

        with pytest.raises(ValidationError):
            Leg(segments=(inbound, lying_outbound), layovers=(layover,))

    def test_builder_raises_when_leg_validator_is_bypassed(self) -> None:
        """Bypass ``Leg``'s constructor validator via ``model_construct``
        (simulating a future caller that skips it — e.g. a buggy mapper
        or a hand-built fixture) and confirm
        ``build_normalized_itinerary`` still raises, rather than silently
        returning a wrong ``total_duration``.
        """
        inbound = _ams_dxb_segment()
        lying_outbound = Segment.model_construct(
            **{
                **_dxb_del_segment().__dict__,
                "duration": timedelta(hours=3, minutes=30),
            }
        )
        layover = _dxb_layover(inbound, lying_outbound)
        broken_leg = Leg.model_construct(segments=(inbound, lying_outbound), layovers=(layover,))

        # Sanity: the endpoints-derived total is unaffected by the lie
        # (it never reads .duration), so a non-defensive caller reading
        # ONLY total_duration would see the correct 14:00 and never know
        # anything was wrong — which is exactly why this must raise
        # instead of silently trusting either number.
        assert broken_leg.total_duration == timedelta(hours=14)

        # A normally-constructed RawOffer(...) would re-run Leg's own
        # model_validator on this nested instance and raise right there
        # (pydantic v2 revalidates nested model fields' "after" validators
        # by default) — which would only prove the domain layer's guard
        # again, not this module's. RawOffer.model_construct(...) bypasses
        # the ENTIRE validation pipeline, including that nested re-check,
        # so the broken leg actually reaches build_normalized_itinerary.
        raw_offer = RawOffer.model_construct(
            provider="mock",
            provider_offer_id="mock-broken",
            legs=(broken_leg,),
            price=Money(amount=Decimal("650.00"), currency="EUR"),
            offer_expires_at=None,
            raw_payload_ref="mock:broken",
            provider_booking_url=None,
        )

        with pytest.raises(NormalizationInvariantError):
            build_normalized_itinerary(
                raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
            )


class TestShapeKeyStability:
    def test_shape_key_stable_across_identical_calls(self) -> None:
        offer_a = _raw_offer(_ams_dxb_del_leg(), provider_offer_id="a")
        offer_b = _raw_offer(_ams_dxb_del_leg(), provider_offer_id="b")

        itinerary_a = build_normalized_itinerary(
            offer_a, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )
        itinerary_b = build_normalized_itinerary(
            offer_b, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        assert itinerary_a.shape_key == itinerary_b.shape_key

    def test_shape_key_differs_when_a_segment_time_changes(self) -> None:
        baseline_offer = _raw_offer(_ams_dxb_del_leg())
        baseline = build_normalized_itinerary(
            baseline_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        shifted_outbound = _dxb_del_segment(depart_utc=datetime(2027, 7, 17, 22, 30, tzinfo=UTC))
        shifted_inbound = _ams_dxb_segment()
        shifted_layover = _dxb_layover(shifted_inbound, shifted_outbound)
        shifted_leg = Leg(segments=(shifted_inbound, shifted_outbound), layovers=(shifted_layover,))
        shifted_offer = _raw_offer(shifted_leg, provider_offer_id="shifted")

        shifted = build_normalized_itinerary(
            shifted_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        assert shifted.shape_key != baseline.shape_key

    def test_shape_key_differs_when_cabin_or_adults_change(self) -> None:
        raw_offer = _raw_offer(_ams_dxb_del_leg())
        baseline = build_normalized_itinerary(
            raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
        )

        different_cabin = build_normalized_itinerary(
            _raw_offer(_ams_dxb_del_leg(), provider_offer_id="cabin-variant"),
            adults=1,
            cabin=CabinClass.BUSINESS,
            fare_as_of=_FARE_AS_OF,
        )
        different_adults = build_normalized_itinerary(
            _raw_offer(_ams_dxb_del_leg(), provider_offer_id="adults-variant"),
            adults=2,
            cabin=CabinClass.ECONOMY,
            fare_as_of=_FARE_AS_OF,
        )

        assert different_cabin.shape_key != baseline.shape_key
        assert different_adults.shape_key != baseline.shape_key


class TestNonEurPriceRaises:
    def test_non_eur_price_raises_instead_of_converting(self) -> None:
        usd_price = Money(amount=Decimal("500.00"), currency="USD")
        raw_offer = _raw_offer(_ams_dxb_del_leg(), price=usd_price)

        with pytest.raises(UnsupportedCurrencyError):
            build_normalized_itinerary(
                raw_offer, adults=1, cabin=CabinClass.ECONOMY, fare_as_of=_FARE_AS_OF
            )
