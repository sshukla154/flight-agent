"""Payload-mapping unit tests for the Amadeus adapter (Phase 7, T49).

Maps the real, hand-authored fixture
(``tests/fixtures/providers/amadeus_offers_sample.json`` -- see that file's
own ``_fixture_note`` and ``tests/fixtures/providers/README.md`` for the
"not a live capture" caveat) through ``providers.amadeus.mapper``'s real
mapping functions and asserts concrete before/after values, not just "it
doesn't crash" -- the two hazards spikes/mapping_sketch.md exists to flag:
ISO-8601 duration parsing and offsetless-local-time-plus-IANA-zone
resolution.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from flightagent.domain.enums import CabinClass
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.providers.amadeus.mapper import (
    extract_fare_option,
    map_offer,
    map_segment,
    parse_iso8601_duration,
)
from flightagent.providers.amadeus.provider import AmadeusProvider
from flightagent.providers.base import CallBudget, FlightProvider
from flightagent.providers.errors import ProviderNotConfigured

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "amadeus_offers_sample.json"
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _offer_by_id(fixture: dict[str, Any], offer_id: str) -> dict[str, Any]:
    return next(offer for offer in fixture["data"] if offer["id"] == offer_id)


def _fare_details_by_segment(offer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        detail["segmentId"]: detail
        for detail in offer["travelerPricings"][0]["fareDetailsBySegment"]
    }


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
            ("PT17H0M", timedelta(hours=17)),  # offer 4's itinerary total -- trailing "0M"
            ("PT9H35M", timedelta(hours=9, minutes=35)),  # offer 4's technical-stop segment
        ],
    )
    def test_parses_amadeus_duration_strings(self, raw: str, expected: timedelta) -> None:
        assert parse_iso8601_duration(raw) == expected

    def test_never_string_compares_equivalent_durations(self) -> None:
        """spikes/mapping_sketch.md S4.1: PT17H0M and PT17H are the same
        duration -- a hand-rolled string-equality check would wrongly treat
        them as different."""
        assert parse_iso8601_duration("PT17H0M") == parse_iso8601_duration("PT17H")


class TestMapSegmentTimezoneHandling:
    """The exact hazard spikes/mapping_sketch.md S4.3 documents: every
    Amadeus timestamp is offsetless local, and naive subtraction of two such
    readings across a timezone change is wrong while still looking
    plausible.
    """

    def test_offer_1_first_segment_ams_to_dxb_resolves_correct_utc(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")
        fare_details = _fare_details_by_segment(offer)
        raw_segment = offer["itineraries"][0]["segments"][0]
        assert raw_segment["departure"]["at"] == "2027-07-17T14:55:00"  # zoneless, on the wire
        assert raw_segment["arrival"]["at"] == "2027-07-17T23:20:00"  # zoneless, on the wire

        segment = map_segment(raw_segment, fare_details)

        # AMS is Europe/Amsterdam CEST (+02:00) in July; DXB is Asia/Dubai
        # (+04:00, no DST) -- both offsets subtracted correctly here.
        assert segment.depart_utc == datetime(2027, 7, 17, 12, 55, tzinfo=UTC)
        assert segment.arrive_utc == datetime(2027, 7, 17, 19, 20, tzinfo=UTC)
        assert segment.duration == timedelta(hours=6, minutes=25)  # matches stated PT6H25M
        assert segment.origin_tz == "Europe/Amsterdam"
        assert segment.destination_tz == "Asia/Dubai"

    def test_offer_1_second_segment_dxb_to_del_resolves_correct_utc(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")
        fare_details = _fare_details_by_segment(offer)
        raw_segment = offer["itineraries"][0]["segments"][1]

        segment = map_segment(raw_segment, fare_details)

        # DXB (+04:00) departs 03:30 on the 18th -> 23:30 UTC on the 17th;
        # DEL (+05:30) arrives 08:20 on the 18th -> 02:50 UTC on the 18th.
        assert segment.depart_utc == datetime(2027, 7, 17, 23, 30, tzinfo=UTC)
        assert segment.arrive_utc == datetime(2027, 7, 18, 2, 50, tzinfo=UTC)
        assert segment.duration == timedelta(hours=3, minutes=20)

    def test_offer_1_itinerary_total_duration_is_13h55_not_naive_17h25(self) -> None:
        """The headline before/after number from spikes/mapping_sketch.md
        S4.3: naive local-time subtraction (14:55 -> 08:20 next day) gives
        17h25. The true, zone-aware elapsed time -- what this mapper
        actually produces via UTC endpoints -- is 13h55, a 3h30 error from
        the +02:00 -> +05:30 offset change alone."""
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")
        fare_details = _fare_details_by_segment(offer)
        raw_itinerary = offer["itineraries"][0]

        naive_depart = datetime.fromisoformat(raw_itinerary["segments"][0]["departure"]["at"])
        naive_arrive = datetime.fromisoformat(raw_itinerary["segments"][-1]["arrival"]["at"])
        naive_wrong_duration = naive_arrive - naive_depart
        assert naive_wrong_duration == timedelta(hours=17, minutes=25)  # BEFORE: the wrong answer

        segments = [map_segment(s, fare_details) for s in raw_itinerary["segments"]]
        correct_duration = segments[-1].arrive_utc - segments[0].depart_utc
        assert correct_duration == timedelta(hours=13, minutes=55)  # AFTER: the correct answer
        assert correct_duration == parse_iso8601_duration(raw_itinerary["duration"])

    def test_offer_3_local_date_rollover_trap(self) -> None:
        """spikes/mapping_sketch.md S4.3's third demonstration: local dates
        roll over (+1) while both UTC instants land on the SAME calendar
        day -- a "+1 day" marker computed from UTC dates would show
        nothing."""
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "3")
        fare_details = _fare_details_by_segment(offer)
        raw_segments = offer["itineraries"][0]["segments"]

        first = map_segment(raw_segments[0], fare_details)
        last = map_segment(raw_segments[-1], fare_details)

        assert first.depart_utc.date() == last.arrive_utc.date()  # UTC: same calendar day
        assert last.arrival_day_offset == 1  # local: correctly shows +1


class TestTechnicalStops:
    def test_offer_4_technical_stop_via_bud_is_counted_not_dropped(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "4")
        fare_details = _fare_details_by_segment(offer)
        raw_segment = offer["itineraries"][0]["segments"][0]  # AMS -> DOH, via BUD
        assert raw_segment["numberOfStops"] == 1

        segment = map_segment(raw_segment, fare_details)
        assert segment.technical_stops == 1
        # The segment's OWN stated duration (PT9H35M) already spans the BUD
        # ground time (README item 3's assumption) -- the endpoints-derived
        # duration must match it exactly, not a "flight time only" reading.
        assert segment.duration == timedelta(hours=9, minutes=35)


class TestOperatingCarrierDefault:
    def test_absent_operating_defaults_to_marketing_carrier(self) -> None:
        """spikes/mapping_sketch.md S2.1 / README item 1: Amadeus MAY omit
        `operating` entirely when it equals the marketing carrier --
        unverified against a live response, so this mapper must default to
        the marketing carrier on absence, never to None (which would
        silently break codeshare detection for every non-codeshare
        segment)."""
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")
        fare_details = _fare_details_by_segment(offer)
        raw_segment = dict(offer["itineraries"][0]["segments"][0])
        del raw_segment["operating"]

        segment = map_segment(raw_segment, fare_details)
        assert segment.operating_carrier == segment.marketing_carrier == "EK"

    def test_present_operating_is_used_verbatim_for_codeshare(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "2")  # QF marketing / EK operating
        fare_details = _fare_details_by_segment(offer)
        raw_segment = offer["itineraries"][0]["segments"][0]

        segment = map_segment(raw_segment, fare_details)
        assert segment.marketing_carrier == "QF"
        assert segment.operating_carrier == "EK"


class TestMapOffer:
    def test_offer_1_maps_to_valid_raw_offer(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")

        raw_offer = map_offer(offer, raw_payload_ref="raw/amadeus/offer-1.json")

        assert raw_offer.provider == "amadeus"
        assert raw_offer.provider_offer_id == "1"
        assert len(raw_offer.legs) == 1
        assert len(raw_offer.legs[0].segments) == 2
        assert raw_offer.price == Money(amount=Decimal("684.31"), currency="EUR")
        assert raw_offer.offer_expires_at is None  # S3.7: no Amadeus equivalent, ever
        assert raw_offer.provider_booking_url is None  # finding 0.6 confirmed
        assert raw_offer.raw_payload_ref == "raw/amadeus/offer-1.json"

    def test_reads_grand_total_not_total(self) -> None:
        """spikes/mapping_sketch.md S4.2: always read `grandTotal`, never
        `total` -- they happen to be equal in this fixture (README item 5),
        so this pins the FIELD read, not merely the resulting value."""
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")
        assert offer["price"]["total"] == offer["price"]["grandTotal"]  # equal here...

        raw_offer = map_offer(offer, raw_payload_ref="ref")
        assert raw_offer.price.amount == Decimal(offer["price"]["grandTotal"])

    def test_offer_4_technical_stop_leg_builds_without_raising(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "4")
        raw_offer = map_offer(offer, raw_payload_ref="ref")
        assert raw_offer.legs[0].total_duration == timedelta(hours=17)

    def test_offer_5_upsell_maps_cleanly_as_its_own_independent_offer(self) -> None:
        """Offer 5 (`isUpsellOffer`) collapses into offer 1 under the dedup
        shape key (normalize/dedup.py, out of scope here) -- but the MAPPER
        itself must still produce a valid, independent `RawOffer` for it."""
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "5")
        raw_offer = map_offer(offer, raw_payload_ref="ref")
        assert raw_offer.price.amount == Decimal("1038.60")


class TestExtractFareOptionBaggageTriState:
    def test_quantity_shaped_baggage_included(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")  # includedCheckedBags: {"quantity": 1}
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("684.31"), currency="EUR")
        )
        assert fare_option.checked_baggage == "included"

    def test_weight_shaped_baggage_included(self) -> None:
        """spikes/mapping_sketch.md S3.2: offer 3 (TK) uses the WEIGHT shape
        (`{"weight": 30, "weightUnit": "KG"}`), mutually exclusive with the
        quantity shape -- the mapper must recognise both."""
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "3")
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("612.44"), currency="EUR")
        )
        assert fare_option.checked_baggage == "included"

    def test_absent_baggage_is_unknown_not_not_included(self) -> None:
        """Construct a fixture-derived offer with NO `includedCheckedBags`
        key at all (the fixture itself never omits it -- every real offer
        here has one shape or the other) to prove absence maps to
        `unknown`, never a silently-invented `not_included`."""
        fixture = _load_fixture()
        offer = json.loads(json.dumps(_offer_by_id(fixture, "1")))  # deep copy
        del offer["travelerPricings"][0]["fareDetailsBySegment"][0]["includedCheckedBags"]

        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("684.31"), currency="EUR")
        )
        assert fare_option.checked_baggage == "unknown"


class TestExtractFareOptionRefundableTriState:
    def test_refundable_true(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "5")  # refundableFare: true
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("1038.60"), currency="EUR")
        )
        assert fare_option.refundable == "allowed"

    def test_refundable_false(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")  # refundableFare: false
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("684.31"), currency="EUR")
        )
        assert fare_option.refundable == "not_allowed"

    def test_refundable_absent_is_unknown(self) -> None:
        fixture = _load_fixture()
        offer = json.loads(json.dumps(_offer_by_id(fixture, "1")))
        del offer["pricingOptions"]["refundableFare"]
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("684.31"), currency="EUR")
        )
        assert fare_option.refundable == "unknown"


class TestExtractFareOptionBrand:
    def test_fare_brand_label_preferred_over_code(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "1")
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("684.31"), currency="EUR")
        )
        assert fare_option.fare_brand == "ECONOMY SAVER"

    def test_fare_brand_absent_is_none(self) -> None:
        fixture = _load_fixture()
        offer = _offer_by_id(fixture, "2")  # no brandedFare/brandedFareLabel at all
        fare_option = extract_fare_option(
            offer, price=Money(amount=Decimal("702.87"), currency="EUR")
        )
        assert fare_option.fare_brand is None


class TestAmadeusProviderProtocolConformance:
    """Matches T9's own isinstance convention -- see
    tests/unit/test_provider_base.py's ``TestFlightProviderProtocolShape``.
    """

    def test_satisfies_flight_provider_protocol(self) -> None:
        assert isinstance(AmadeusProvider(), FlightProvider)

    def test_capabilities_shape(self) -> None:
        caps = AmadeusProvider().capabilities
        assert caps.provider_name == "amadeus"
        assert caps.api_version == "v2"
        assert caps.auth_style == "oauth2_client_credentials"
        assert caps.returns_booking_url is False
        assert caps.stop_filter_style == "nonstop_boolean"


class TestAmadeusProviderRaisesNotConfigured:
    def test_search_raises_provider_not_configured(self) -> None:
        provider = AmadeusProvider()
        with pytest.raises(ProviderNotConfigured):
            asyncio.run(provider.search(_make_request(), CallBudget()))

    def test_search_raises_even_with_credentials_supplied(self) -> None:
        """D6: no OAuth2 flow is wired up regardless of what is passed to
        __init__ -- supplying credentials does not make this adapter start
        working."""
        provider = AmadeusProvider(client_id="x", client_secret="y")
        with pytest.raises(ProviderNotConfigured):
            asyncio.run(provider.search(_make_request(), CallBudget()))

    def test_raised_error_carries_the_provider_name(self) -> None:
        provider = AmadeusProvider()
        try:
            asyncio.run(provider.search(_make_request(), CallBudget()))
        except ProviderNotConfigured as exc:
            assert exc.provider == "amadeus"
        else:
            pytest.fail("expected ProviderNotConfigured to be raised")
