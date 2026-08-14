"""Tests for MockProvider (Phase 2 / T10).

Master plan S5's mock-provider determinism subsection is why this file
exists: programmatic-mode ``search()`` must be a pure function of the
request's canonical fields, seeded fresh per call from a request-derived
hash -- never a shared ``random.Random`` -- so a "deterministic" mock run
stays deterministic under any future concurrent fan-out. See
``generator.py``'s own module docstring for the full rationale.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import flightagent.providers.mock as mock_package
import flightagent.providers.mock.provider as provider_module
from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import RawOffer
from flightagent.domain.run import SearchRequest
from flightagent.providers.base import CallBudget, FlightProvider, ProviderSearchResult
from flightagent.providers.mock import MockProvider
from flightagent.providers.mock.generator import compute_seed

FIXTURE_PATH = Path(mock_package.__file__).parent / "fixtures" / "ams_del_onestop.json"


def _make_request(
    *,
    origin: str = "AMS",
    destination: str = "DEL",
    departure_date: date = date(2027, 7, 17),
    max_stops: int = 1,
    adults: int = 1,
    currency: str = "EUR",
) -> SearchRequest:
    return SearchRequest(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        cabin=CabinClass.ECONOMY,
        max_stops=max_stops,  # type: ignore[arg-type]
        adults=adults,
        currency=currency,
        layover_min=timedelta(minutes=180),
        layover_max=timedelta(minutes=360),
    )


def _run_search(provider: MockProvider, request: SearchRequest) -> ProviderSearchResult:
    return asyncio.run(provider.search(request, CallBudget()))


def _layover_minutes(offer: RawOffer) -> list[float]:
    return [
        layover.duration.total_seconds() / 60 for leg in offer.legs for layover in leg.layovers
    ]


class TestMockProviderProtocolConformance:
    def test_satisfies_flight_provider_protocol(self) -> None:
        assert isinstance(MockProvider(), FlightProvider)

    def test_capabilities_shape(self) -> None:
        caps = MockProvider().capabilities
        assert caps.provider_name == "mock"
        assert caps.auth_style == "none"
        assert caps.paginated is False
        assert caps.returns_booking_url is True
        assert caps.stop_filter_style == "nonstop_boolean"


class TestComputeSeed:
    def test_identical_canonical_fields_produce_the_same_seed(self) -> None:
        assert compute_seed(_make_request()) == compute_seed(_make_request())

    def test_different_adults_change_the_seed(self) -> None:
        assert compute_seed(_make_request(adults=1)) != compute_seed(_make_request(adults=2))

    def test_different_departure_dates_change_the_seed(self) -> None:
        seed_a = compute_seed(_make_request(departure_date=date(2027, 7, 17)))
        seed_b = compute_seed(_make_request(departure_date=date(2027, 7, 18)))
        assert seed_a != seed_b


class TestProgrammaticModeDeterminism:
    """The task's core determinism proof: identical requests -> identical
    output, byte for byte, and this must hold regardless of provider
    instance identity or of other, unrelated calls happening in between --
    exactly the property a shared module-level RNG would violate.
    """

    def test_identical_requests_produce_byte_identical_results(self) -> None:
        provider = MockProvider()
        request_a = _make_request()
        request_b = _make_request()  # independently constructed, same fields
        assert request_a == request_b  # sanity: not relying on object identity

        result_a = _run_search(provider, request_a)
        result_b = _run_search(provider, request_b)

        assert result_a.model_dump_json() == result_b.model_dump_json()
        assert result_a.offers == result_b.offers
        assert len(result_a.offers) > 0

    def test_two_separate_provider_instances_agree(self) -> None:
        """Determinism must not depend on reusing one ``MockProvider`` (or
        an RNG living on it) across calls -- a fresh provider each time
        proves the seed is a function of the request, not of instance
        state."""
        request = _make_request()
        result_a = _run_search(MockProvider(), request)
        result_b = _run_search(MockProvider(), request)
        assert result_a.model_dump_json() == result_b.model_dump_json()

    def test_result_is_unaffected_by_interleaved_different_requests(self) -> None:
        """This is the test a shared module-level ``random.Random`` would
        fail: if the generator consumed global RNG state, searching two
        OTHER destinations in between would shift that shared state and
        change ``request_a``'s own output the second time round, even
        though ``request_a`` itself never changed."""
        provider = MockProvider()
        request_a = _make_request(destination="DEL")

        result_before = _run_search(provider, request_a)
        _run_search(provider, _make_request(destination="BOM"))
        _run_search(provider, _make_request(destination="BLR"))
        result_after = _run_search(provider, request_a)

        assert result_before.model_dump_json() == result_after.model_dump_json()

    def test_different_destinations_produce_different_results(self) -> None:
        provider = MockProvider()
        result_del = _run_search(provider, _make_request(destination="DEL"))
        result_bom = _run_search(provider, _make_request(destination="BOM"))

        assert result_del.model_dump_json() != result_bom.model_dump_json()
        assert result_del.offers != result_bom.offers

    def test_different_origins_produce_different_results(self) -> None:
        provider = MockProvider()
        result_ams = _run_search(provider, _make_request(origin="AMS"))
        result_dus = _run_search(provider, _make_request(origin="DUS"))
        assert result_ams.model_dump_json() != result_dus.model_dump_json()

    def test_different_departure_dates_produce_different_results(self) -> None:
        provider = MockProvider()
        result_a = _run_search(provider, _make_request(departure_date=date(2027, 7, 17)))
        result_b = _run_search(provider, _make_request(departure_date=date(2027, 7, 18)))
        assert result_a.model_dump_json() != result_b.model_dump_json()


class TestProgrammaticModeOfferShape:
    def test_max_stops_zero_produces_only_direct_offers(self) -> None:
        result = _run_search(MockProvider(), _make_request(max_stops=0))
        assert len(result.offers) > 0
        for offer in result.offers:
            assert len(offer.legs) == 1
            assert len(offer.legs[0].segments) == 1
            assert offer.legs[0].segments[0].origin == "AMS"
            assert offer.legs[0].segments[0].destination == "DEL"
            assert offer.legs[0].layovers == ()

    def test_max_stops_one_always_includes_an_offer_with_a_valid_layover(self) -> None:
        """The offer the CLI needs to actually recommend something (master
        plan S5): at least one one-stop offer with a layover inside the
        request's own ``[layover_min, layover_max]`` window."""
        result = _run_search(MockProvider(), _make_request(max_stops=1))
        assert len(result.offers) > 0

        valid_layovers = [
            minutes
            for offer in result.offers
            for minutes in _layover_minutes(offer)
            if 180 <= minutes <= 360
        ]
        assert len(valid_layovers) >= 1

    def test_max_stops_one_offers_are_all_one_stop(self) -> None:
        result = _run_search(MockProvider(), _make_request(max_stops=1))
        for offer in result.offers:
            assert len(offer.legs) == 1
            assert len(offer.legs[0].segments) == 2
            assert len(offer.legs[0].layovers) == 1

    def test_offers_carry_real_iata_codes_matching_the_request(self) -> None:
        request = _make_request(origin="EIN", destination="BLR", max_stops=1)
        result = _run_search(MockProvider(), request)
        for offer in result.offers:
            segments = offer.legs[0].segments
            assert segments[0].origin == "EIN"
            assert segments[-1].destination == "BLR"


class TestFixtureFileMode:
    def test_fixture_file_mode_returns_committed_file_verbatim(self) -> None:
        provider = MockProvider(fixture_path=FIXTURE_PATH)
        result = _run_search(provider, _make_request())

        raw_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        expected_offers = tuple(RawOffer.model_validate(item) for item in raw_json["offers"])

        assert result.offers == expected_offers
        assert len(result.offers) == 3

    def test_fixture_file_mode_ignores_the_request(self) -> None:
        """Fixture-file mode returns the file's own AMS->DEL offers even
        when the request asks for a different route -- proving the
        request is never consulted to filter or alter the fixture."""
        provider = MockProvider(fixture_path=FIXTURE_PATH)
        del_result = _run_search(provider, _make_request(destination="DEL"))
        bom_result = _run_search(provider, _make_request(destination="BOM"))

        assert del_result.offers == bom_result.offers
        assert all(offer.legs[0].segments[-1].destination == "DEL" for offer in del_result.offers)

    def test_fixture_file_mode_never_calls_the_generator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No randomness at all in fixture mode -- ``generate_offers`` must
        never be invoked when a ``fixture_path`` is configured."""

        def _fail(*args: object, **kwargs: object) -> tuple[RawOffer, ...]:
            raise AssertionError("generate_offers must not be called in fixture-file mode")

        monkeypatch.setattr(provider_module, "generate_offers", _fail)
        provider = MockProvider(fixture_path=FIXTURE_PATH)
        result = _run_search(provider, _make_request())
        assert len(result.offers) == 3


class TestAmsDelFixtureScenario:
    def test_all_offers_are_the_ams_dxb_del_route(self) -> None:
        raw_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        offers = [RawOffer.model_validate(item) for item in raw_json["offers"]]
        assert len(offers) >= 2  # need at least one valid + one invalid, per the task brief
        for offer in offers:
            segments = offer.legs[0].segments
            assert segments[0].origin == "AMS"
            assert segments[-1].destination == "DEL"
            assert len(offer.legs[0].layovers) == 1

    def test_contains_at_least_one_offer_within_the_valid_layover_window(self) -> None:
        raw_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        offers = [RawOffer.model_validate(item) for item in raw_json["offers"]]
        all_minutes = [minutes for offer in offers for minutes in _layover_minutes(offer)]
        assert any(180 <= minutes <= 360 for minutes in all_minutes)

    def test_contains_at_least_one_offer_outside_the_valid_layover_window(self) -> None:
        raw_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        offers = [RawOffer.model_validate(item) for item in raw_json["offers"]]
        all_minutes = [minutes for offer in offers for minutes in _layover_minutes(offer)]
        assert any(minutes < 180 or minutes > 360 for minutes in all_minutes)
