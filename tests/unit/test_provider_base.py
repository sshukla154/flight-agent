"""Tests for the FlightProvider protocol and the provider error taxonomy
(Phase 2 / T9).

Two things this file has to prove, matching the task's own framing:

1. ``FlightProvider`` behaves like a real structural contract — a
   Protocol class itself cannot be instantiated, a class implementing its
   full shape satisfies ``isinstance`` against it, and a class missing
   either member does not (the negative cases matter at least as much as
   the positive one: an ``isinstance`` check that always returns True
   would pass the positive test for the wrong reason).
2. Each concrete error class carries the ``Retryability`` the master plan
   assigns it, and the base class carries none — an unclassified error
   must fail loudly, not silently inherit a plausible-looking default.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, RawOffer
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest
from flightagent.domain.segment import Segment
from flightagent.providers.base import (
    CallBudget,
    FlightProvider,
    ProviderCapabilities,
    ProviderSearchResult,
)
from flightagent.providers.errors import (
    ProviderConfigError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    Retryability,
)


def _make_search_request() -> SearchRequest:
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


def _make_segment() -> Segment:
    """AMS (Europe/Amsterdam, CEST=+2) -> DXB (Asia/Dubai, +4) in July — same
    default shape as test_domain_smoke.py's helper, proven consistent with
    Segment's tz-consistency validator."""
    depart_utc = datetime(2027, 7, 17, 10, 0, tzinfo=UTC)
    arrive_utc = datetime(2027, 7, 17, 18, 0, tzinfo=UTC)
    return Segment(
        segment_id="AMS-DXB-429",
        origin="AMS",
        destination="DXB",
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(ZoneInfo("Europe/Amsterdam")),
        arrive_local=arrive_utc.astimezone(ZoneInfo("Asia/Dubai")),
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
        marketing_carrier="KL",
        flight_number="429",
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _make_raw_offer() -> RawOffer:
    leg = Leg(segments=(_make_segment(),), layovers=())
    return RawOffer(
        provider="mock",
        provider_offer_id="offer-0001",
        legs=(leg,),
        price=Money(amount=Decimal("650.00"), currency="EUR"),
        raw_payload_ref="raw/mock/offer-0001.json",
    )


def _make_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_name="mock",
        api_version="v1",
        auth_style="none",
        paginated=False,
        native_currency_forceable=True,
        returns_booking_url=True,
        stop_filter_style="nonstop_boolean",
    )


class TestFlightProviderProtocolShape:
    def test_protocol_cannot_be_instantiated_directly(self) -> None:
        with pytest.raises(TypeError):
            FlightProvider()  # type: ignore[abstract]

    def test_conforming_class_satisfies_isinstance(self) -> None:
        class ConformingProvider:
            @property
            def capabilities(self) -> ProviderCapabilities:
                return _make_capabilities()

            async def search(
                self, request: SearchRequest, budget: CallBudget
            ) -> ProviderSearchResult:
                return ProviderSearchResult(truncated=False, pages_fetched=1, http_calls=1)

        provider = ConformingProvider()
        assert isinstance(provider, FlightProvider)

        result = asyncio.run(provider.search(_make_search_request(), CallBudget()))
        assert result.http_calls == 1

    def test_class_missing_search_does_not_satisfy_isinstance(self) -> None:
        class MissingSearch:
            @property
            def capabilities(self) -> ProviderCapabilities:
                return _make_capabilities()

        assert not isinstance(MissingSearch(), FlightProvider)

    def test_class_missing_capabilities_does_not_satisfy_isinstance(self) -> None:
        class MissingCapabilities:
            async def search(
                self, request: SearchRequest, budget: CallBudget
            ) -> ProviderSearchResult:
                return ProviderSearchResult(truncated=False, pages_fetched=0, http_calls=0)

        assert not isinstance(MissingCapabilities(), FlightProvider)

    def test_unrelated_class_does_not_satisfy_isinstance(self) -> None:
        class Unrelated:
            pass

        assert not isinstance(Unrelated(), FlightProvider)


class TestCallBudget:
    def test_defaults_match_master_plan_s5(self) -> None:
        budget = CallBudget()
        assert budget.timeout == timedelta(seconds=20)
        assert budget.max_pages == 3

    def test_rejects_non_positive_timeout(self) -> None:
        with pytest.raises(ValidationError):
            CallBudget(timeout=timedelta(0))

    def test_rejects_zero_max_pages(self) -> None:
        with pytest.raises(ValidationError):
            CallBudget(max_pages=0)

    def test_frozen(self) -> None:
        budget = CallBudget()
        with pytest.raises(ValidationError):
            budget.max_pages = 5  # type: ignore[misc]


class TestProviderCapabilities:
    def test_extra_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderCapabilities(
                provider_name="mock",
                api_version="v1",
                auth_style="none",
                paginated=False,
                native_currency_forceable=True,
                returns_booking_url=True,
                stop_filter_style="nonstop_boolean",
                unexpected_field="oops",  # type: ignore[call-arg]
            )


class TestProviderSearchResult:
    def test_constructs_with_offers(self) -> None:
        offer = _make_raw_offer()
        result = ProviderSearchResult(
            offers=(offer,),
            truncated=False,
            pages_fetched=1,
            http_calls=1,
            raw_payload_refs=("raw/mock/offer-0001.json",),
        )
        assert result.offers == (offer,)
        assert result.provider_warnings == ()

    def test_empty_offers_defaults_are_empty_tuples(self) -> None:
        result = ProviderSearchResult(truncated=False, pages_fetched=1, http_calls=1)
        assert result.offers == ()
        assert result.raw_payload_refs == ()
        assert result.provider_warnings == ()

    def test_rejects_negative_pages_fetched(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSearchResult(truncated=False, pages_fetched=-1, http_calls=0)

    def test_rejects_negative_http_calls(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSearchResult(truncated=False, pages_fetched=0, http_calls=-1)


class TestRetryabilityTagging:
    def test_provider_error_base_has_no_default_retryability(self) -> None:
        with pytest.raises(AttributeError):
            _ = ProviderError.retryability

    @pytest.mark.parametrize(
        ("error_cls", "expected"),
        [
            (ProviderTimeoutError, Retryability.TRANSIENT),
            (ProviderRateLimitedError, Retryability.TRANSIENT),
            (ProviderConfigError, Retryability.PERMANENT),
            (ProviderNotConfigured, Retryability.PERMANENT),
        ],
    )
    def test_error_class_carries_expected_retryability(
        self, error_cls: type[ProviderError], expected: Retryability
    ) -> None:
        assert error_cls.retryability == expected


class TestProviderErrorHierarchy:
    def test_provider_not_configured_is_a_provider_config_error(self) -> None:
        assert issubclass(ProviderNotConfigured, ProviderConfigError)
        assert issubclass(ProviderConfigError, ProviderError)

    def test_provider_timeout_error_carries_message_and_provider(self) -> None:
        error = ProviderTimeoutError("no response in 20s", provider="amadeus")
        assert str(error) == "no response in 20s"
        assert error.provider == "amadeus"
        assert isinstance(error, ProviderError)

    def test_provider_rate_limited_error_carries_retry_after(self) -> None:
        error = ProviderRateLimitedError(
            "429 from provider", provider="amadeus", retry_after=timedelta(seconds=30)
        )
        assert error.retry_after == timedelta(seconds=30)

    def test_provider_rate_limited_error_retry_after_defaults_to_none(self) -> None:
        error = ProviderRateLimitedError("429 from provider", provider="duffel")
        assert error.retry_after is None

    def test_provider_not_configured_carries_no_retry_after(self) -> None:
        error = ProviderNotConfigured("AMADEUS_CLIENT_ID is not set", provider="amadeus")
        assert error.provider == "amadeus"
        assert isinstance(error, ProviderConfigError)
