"""Payload-mapping unit tests for the Duffel adapter (Phase 7, T49).

Maps the real, hand-authored fixture
(``tests/fixtures/providers/duffel_offers_sample.json`` -- see that file's
own ``_fixture_note``: "a real capture of either endpoint is only the inner
object", i.e. read ``fixture["offers_list_response"]["data"]``, never the
root) through ``providers.duffel.mapper``'s real mapping functions, asserting
concrete before/after values for the same two hazards
``test_amadeus_mapping.py`` exercises, plus the Duffel-specific shape
differences spikes/mapping_sketch.md S2 documents (tri-state refundability,
non-EUR pricing, the inline-timezone consistency check).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from flightagent.domain.enums import CabinClass
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.providers.base import CallBudget, FlightProvider
from flightagent.providers.duffel.mapper import (
    extract_fare_option,
    map_offer,
    map_segment,
    parse_iso8601_duration,
)
from flightagent.providers.duffel.provider import DuffelProvider
from flightagent.providers.errors import ProviderNotConfigured

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "duffel_offers_sample.json"
)


def _load_offers() -> list[dict[str, Any]]:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    # _fixture_note: "a real capture of either endpoint is only the inner
    # object" -- offers_list_response.data is the actual GET /air/offers body.
    offers: list[dict[str, Any]] = fixture["offers_list_response"]["data"]
    return offers


def _offer_by_id(offer_id: str) -> dict[str, Any]:
    return next(offer for offer in _load_offers() if offer["id"] == offer_id)


def _make_request() -> SearchRequest:
    return SearchRequest(
        origin="AMS",
        destination="DEL",
        departure_date=date(2027, 7, 17),
        cabin=CabinClass.ECONOMY,
        max_stops=1,
        currency="EUR",
        layover_min=timedelta(minutes=180),
        layover_max=timedelta(minutes=360),
    )


class TestParseIso8601Duration:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PT6H25M", timedelta(hours=6, minutes=25)),
            ("PT13H55M", timedelta(hours=13, minutes=55)),
            ("PT15H25M", timedelta(hours=15, minutes=25)),
            ("PT1H0M", timedelta(hours=1)),  # the TBS technical-stop duration
        ],
    )
    def test_parses_duffel_duration_strings(self, raw: str, expected: timedelta) -> None:
        assert parse_iso8601_duration(raw) == expected


class TestMapSegmentTimezoneHandling:
    def test_offer_0001_first_segment_ams_to_dxb_resolves_correct_utc(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000001")
        raw_segment = offer["slices"][0]["segments"][0]
        assert raw_segment["departing_at"] == "2027-07-17T14:55:00"  # zoneless, on the wire

        segment = map_segment(raw_segment)

        assert segment.depart_utc == datetime(2027, 7, 17, 12, 55, tzinfo=UTC)
        assert segment.arrive_utc == datetime(2027, 7, 17, 19, 20, tzinfo=UTC)
        assert segment.duration == timedelta(hours=6, minutes=25)

    def test_offer_0001_slice_total_duration_is_13h55_not_naive_17h25(self) -> None:
        """Identical before/after demonstration to
        test_amadeus_mapping.py's own -- offer 0001 shares AMS-DXB-DEL's
        exact times with Amadeus offer 1 (tests/fixtures/providers/README.md)."""
        offer = _offer_by_id("off_0000AbCdEf000000000001")
        raw_segments = offer["slices"][0]["segments"]

        naive_depart = datetime.fromisoformat(raw_segments[0]["departing_at"])
        naive_arrive = datetime.fromisoformat(raw_segments[-1]["arriving_at"])
        assert naive_arrive - naive_depart == timedelta(hours=17, minutes=25)  # BEFORE (wrong)

        segments = [map_segment(s) for s in raw_segments]
        correct_duration = segments[-1].arrive_utc - segments[0].depart_utc
        assert correct_duration == timedelta(hours=13, minutes=55)  # AFTER (correct)
        assert correct_duration == parse_iso8601_duration(offer["slices"][0]["duration"])

    def test_offer_0003_local_date_rollover_trap(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000003")
        raw_segments = offer["slices"][0]["segments"]

        first = map_segment(raw_segments[0])
        last = map_segment(raw_segments[-1])

        assert first.depart_utc.date() == last.arrive_utc.date()  # UTC: same calendar day
        assert last.arrival_day_offset == 1  # local: correctly shows +1

    def test_tz_mismatch_against_duffel_inline_hint_is_logged_not_trusted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """spikes/mapping_sketch.md S1.1/S4.3: Duffel's inline `time_zone`
        is a consistency CHECK, never the authority -- a mismatch must be
        logged, and the catalog's own resolution must still win."""
        offer = _offer_by_id("off_0000AbCdEf000000000001")
        raw_segment = json.loads(json.dumps(offer["slices"][0]["segments"][0]))
        raw_segment["origin"]["time_zone"] = "Not/AZone"

        with caplog.at_level(logging.WARNING):
            segment = map_segment(raw_segment)

        assert segment.origin_tz == "Europe/Amsterdam"  # catalog wins, not the bad hint
        assert any("disagrees" in message for message in caplog.messages)


class TestTechnicalStops:
    def test_offer_0003_technical_stop_via_tbs_is_counted(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000003")
        raw_segment = offer["slices"][0]["segments"][1]  # IST -> DEL, via TBS
        assert len(raw_segment["stops"]) == 1

        segment = map_segment(raw_segment)
        assert segment.technical_stops == 1
        assert segment.duration == timedelta(hours=6, minutes=50)


class TestCodeshareFields:
    def test_offer_0002_marketing_and_operating_carrier_and_flight_numbers(self) -> None:
        """spikes/mapping_sketch.md S2.1: Duffel gives an operating FLIGHT
        NUMBER too, which Amadeus cannot -- QF 8148 marketed, operated as
        EK 148."""
        offer = _offer_by_id("off_0000AbCdEf000000000002")
        raw_segment = offer["slices"][0]["segments"][0]

        segment = map_segment(raw_segment)
        assert segment.marketing_carrier == "QF"
        assert segment.flight_number == "8148"
        assert segment.operating_carrier == "EK"
        assert segment.operating_flight_number == "148"


class TestMapOffer:
    def test_offer_0001_maps_to_valid_raw_offer(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000001")
        raw_offer = map_offer(offer, raw_payload_ref="raw/duffel/off-0001.json")

        assert raw_offer.provider == "duffel"
        assert raw_offer.provider_offer_id == "off_0000AbCdEf000000000001"
        assert raw_offer.price == Money(amount=Decimal("689.00"), currency="EUR")
        assert raw_offer.offer_expires_at == datetime(2026, 8, 14, 9, 42, 5, 118904, tzinfo=UTC)
        assert raw_offer.provider_booking_url is None
        assert raw_offer.raw_payload_ref == "raw/duffel/off-0001.json"

    def test_offer_0003_is_non_eur_and_never_silently_converted(self) -> None:
        """D14/spikes/mapping_sketch.md S2.4: Duffel returns the currency
        the airline actually priced in -- USD here, in an otherwise-EUR
        result set. The mapper must carry it through verbatim, never
        coerce."""
        offer = _offer_by_id("off_0000AbCdEf000000000003")
        raw_offer = map_offer(offer, raw_payload_ref="ref")
        assert raw_offer.price.currency == "USD"
        assert raw_offer.price.amount == Decimal("664.20")

    def test_offer_expires_at_is_none_when_absent(self) -> None:
        offer = json.loads(json.dumps(_offer_by_id("off_0000AbCdEf000000000001")))
        offer["expires_at"] = None
        raw_offer = map_offer(offer, raw_payload_ref="ref")
        assert raw_offer.offer_expires_at is None


class TestExtractFareOptionBaggageTriState:
    def test_checked_bag_quantity_one_is_included(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000001")
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("689.00"), currency="EUR")
        )
        assert fare_option.checked_baggage == "included"

    def test_checked_bag_quantity_zero_is_not_included_not_unknown(self) -> None:
        """spikes/mapping_sketch.md S3.2: offer 0003 is genuinely
        hand-baggage-only (`checked` quantity 0) -- a confirmed zero, never
        rendered the same as a provider that simply didn't say."""
        offer = _offer_by_id("off_0000AbCdEf000000000003")
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("664.20"), currency="USD")
        )
        assert fare_option.checked_baggage == "not_included"

    def test_no_checked_entry_at_all_is_unknown(self) -> None:
        offer = json.loads(json.dumps(_offer_by_id("off_0000AbCdEf000000000001")))
        passenger = offer["slices"][0]["segments"][0]["passengers"][0]
        passenger["baggages"] = [bag for bag in passenger["baggages"] if bag["type"] != "checked"]

        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("689.00"), currency="EUR")
        )
        assert fare_option.checked_baggage == "unknown"


class TestExtractFareOptionRefundableTriState:
    def test_refundable_false_is_not_allowed(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000001")  # refund_before_departure.allowed: false
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("689.00"), currency="EUR")
        )
        assert fare_option.refundable == "not_allowed"

    def test_refundable_true_with_penalty_is_allowed(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000004")  # refundable, 75.00 EUR penalty
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("1042.00"), currency="EUR")
        )
        assert fare_option.refundable == "allowed"

    def test_refundable_null_is_unknown_not_false(self) -> None:
        """spikes/mapping_sketch.md S3.6's trap: Duffel's `allowed: null`
        means "the airline didn't tell us", not "no". Collapsing it to
        `not_allowed` would invent a restriction that may not exist. The
        fixture never actually emits `null` here (every offer states
        true/false) -- this constructs that case explicitly, since it is
        the whole point of the tri-state field."""
        offer = json.loads(json.dumps(_offer_by_id("off_0000AbCdEf000000000001")))
        offer["conditions"]["refund_before_departure"]["allowed"] = None
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("689.00"), currency="EUR")
        )
        assert fare_option.refundable == "unknown"


class TestExtractFareOptionBrand:
    def test_fare_brand_name_is_carried(self) -> None:
        offer = _offer_by_id("off_0000AbCdEf000000000001")
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("689.00"), currency="EUR")
        )
        assert fare_option.fare_brand == "Economy Saver"


class TestDuffelProviderProtocolConformance:
    """Matches T9's own isinstance convention -- see
    tests/unit/test_provider_base.py's ``TestFlightProviderProtocolShape``.
    """

    def test_satisfies_flight_provider_protocol(self) -> None:
        assert isinstance(DuffelProvider(), FlightProvider)

    def test_capabilities_shape(self) -> None:
        caps = DuffelProvider().capabilities
        assert caps.provider_name == "duffel"
        assert caps.api_version == "v2"
        assert caps.auth_style == "static_bearer"
        assert caps.native_currency_forceable is False
        assert caps.returns_booking_url is False
        assert caps.stop_filter_style == "max_connections_param"


class TestDuffelProviderRaisesNotConfigured:
    def test_search_raises_provider_not_configured(self) -> None:
        provider = DuffelProvider()
        with pytest.raises(ProviderNotConfigured):
            asyncio.run(provider.search(_make_request(), CallBudget()))

    def test_search_raises_even_with_api_key_supplied(self) -> None:
        """D6: no static-bearer credential is ever attached to a request
        regardless of what is passed to __init__."""
        provider = DuffelProvider(api_key="duffel_test_x")
        with pytest.raises(ProviderNotConfigured):
            asyncio.run(provider.search(_make_request(), CallBudget()))

    def test_raised_error_carries_the_provider_name(self) -> None:
        provider = DuffelProvider()
        try:
            asyncio.run(provider.search(_make_request(), CallBudget()))
        except ProviderNotConfigured as exc:
            assert exc.provider == "duffel"
        else:
            pytest.fail("expected ProviderNotConfigured to be raised")
