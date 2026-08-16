"""Unit tests for flightagent.reporting (T15, report writer v1).

Master plan S8.6 (CRITICAL): mock output must be unmistakably synthetic --
a banner at the very top of the Markdown, and ``"data_source": "mock"`` as
a structural top-level JSON field. ``TestSyntheticDataBanner`` below checks
the banner's POSITION, not just its presence -- a banner buried at the
bottom would satisfy a naive "is it in there somewhere" check while still
failing the actual requirement.

Master plan S8.2 (CRITICAL, master plan S8.8 checklist): a booking URL
must never be mistaken for a real one. ``TestBookingUrlValidator`` and
``TestRenderedBookingLinkSafety`` prove both that the validator itself
rejects the right things, AND that the renderer never lets a rejected URL
leak into the rendered document as raw, clickable text -- a validator that
exists but isn't actually wired into the render path would still pass a
naive "does validate_booking_url raise" test while leaving the real
report unsafe.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import jsonschema
import pytest

from flightagent.domain.enums import CabinClass, TaskState
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.run import TaskOutcome
from flightagent.domain.scoring import ScoreComponents, ScoredItinerary
from flightagent.domain.segment import Layover, Segment
from flightagent.reporting.booking_link import BookingUrlRejected, validate_booking_url
from flightagent.reporting.json_report import build_results_document
from flightagent.reporting.markdown import SYNTHETIC_DATA_BANNER, render_markdown_report
from flightagent.reporting.writer import atomic_write_text, write_report_artifacts

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config" / "results.schema.json"

_DEPARTURE_DATE = date(2027, 7, 17)
_GENERATED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

_MOCK_BOOKING_URL = "https://mock.flightagent.invalid/booking/top-itin"
_MOCK_BOOKING_URL_SECOND = "https://mock.flightagent.invalid/booking/second-itin"
_INSECURE_BOOKING_URL = "http://insecure.example.com/should-not-appear"

_BookingUrlKind = Literal["provider_native", "search_deeplink", "unavailable"]


def _segment(
    *,
    origin: str,
    destination: str,
    depart_utc: datetime,
    arrive_utc: datetime,
    origin_tz: str,
    destination_tz: str,
    marketing_carrier: str,
    flight_number: str,
) -> Segment:
    return Segment(
        segment_id=f"{origin}-{destination}-{flight_number}",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(ZoneInfo(origin_tz)),
        arrive_local=arrive_utc.astimezone(ZoneInfo(destination_tz)),
        origin_tz=origin_tz,
        destination_tz=destination_tz,
        marketing_carrier=marketing_carrier,
        flight_number=flight_number,
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _layover(*, airport: str, arrive_utc: datetime, depart_utc: datetime, tz_name: str) -> Layover:
    zone = ZoneInfo(tz_name)
    return Layover(
        airport=airport,
        arrive_utc=arrive_utc,
        depart_utc=depart_utc,
        duration=depart_utc - arrive_utc,
        local_window=(arrive_utc.astimezone(zone), depart_utc.astimezone(zone)),
        requires_airport_change=False,
        requires_terminal_change=False,
    )


def _one_stop_itinerary(
    *,
    itinerary_id: str,
    price_eur: Decimal,
    carrier: str,
    hub: str,
    hub_tz: str,
    layover_minutes: int,
    booking_url: str | None,
    booking_url_kind: _BookingUrlKind = "provider_native",
) -> NormalizedItinerary:
    """AMS -(carrier)-> hub -(carrier)-> DEL, one layover of exactly
    ``layover_minutes``. Real IANA zones throughout, matching the shape
    ``providers.mock.generator`` itself produces."""
    origin_tz = "Europe/Amsterdam"
    destination_tz = "Asia/Kolkata"

    depart_utc_origin = datetime(2027, 7, 17, 7, 0, tzinfo=UTC)
    arrive_utc_hub = depart_utc_origin + timedelta(hours=6)
    depart_utc_hub = arrive_utc_hub + timedelta(minutes=layover_minutes)
    arrive_utc_dest = depart_utc_hub + timedelta(hours=4)

    seg1 = _segment(
        origin="AMS",
        destination=hub,
        depart_utc=depart_utc_origin,
        arrive_utc=arrive_utc_hub,
        origin_tz=origin_tz,
        destination_tz=hub_tz,
        marketing_carrier=carrier,
        flight_number="101",
    )
    layover = _layover(
        airport=hub, arrive_utc=arrive_utc_hub, depart_utc=depart_utc_hub, tz_name=hub_tz
    )
    seg2 = _segment(
        origin=hub,
        destination="DEL",
        depart_utc=depart_utc_hub,
        arrive_utc=arrive_utc_dest,
        origin_tz=hub_tz,
        destination_tz=destination_tz,
        marketing_carrier=carrier,
        flight_number="202",
    )
    leg = Leg(segments=(seg1, seg2), layovers=(layover,))
    price = Money(amount=price_eur, currency="EUR")

    return NormalizedItinerary(
        itinerary_id=itinerary_id,
        provider="mock",
        legs=(leg,),
        price_original=price,
        price_eur=price,
        booking_url=booking_url,
        booking_url_kind=booking_url_kind,
        shape_key=f"shape-{itinerary_id}",
        fare_as_of=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
    )


def _scored(
    itinerary: NormalizedItinerary,
    *,
    rank: int,
    fare_component: Decimal,
    elapsed_component: Decimal = Decimal("0"),
    layover_penalty: Decimal = Decimal("0"),
) -> ScoredItinerary:
    components = ScoreComponents(
        fare_eur=fare_component,
        elapsed_time_component=elapsed_component,
        layover_penalty=layover_penalty,
        direct_bonus=Decimal("0"),
    )
    return ScoredItinerary(
        itinerary=itinerary,
        components=components,
        rank_by_adjusted_score=rank,
        rank_by_total_journey_score=rank,
        rank_by_price=rank,
    )


def _top_itinerary(*, booking_url: str | None = _MOCK_BOOKING_URL) -> NormalizedItinerary:
    return _one_stop_itinerary(
        itinerary_id="itin_top_0001",
        price_eur=Decimal("725.00"),
        carrier="EK",
        hub="DXB",
        hub_tz="Asia/Dubai",
        layover_minutes=250,  # 4h 10m -- spec section 6's own example, matched literally.
        booking_url=booking_url,
    )


def _second_itinerary() -> NormalizedItinerary:
    return _one_stop_itinerary(
        itinerary_id="itin_second_0001",
        price_eur=Decimal("780.00"),
        carrier="TK",
        hub="IST",
        hub_tz="Europe/Istanbul",
        layover_minutes=200,
        booking_url=_MOCK_BOOKING_URL_SECOND,
    )


def _ranked_pair() -> list[ScoredItinerary]:
    top = _scored(
        _top_itinerary(),
        rank=1,
        fare_component=Decimal("725.00"),
        elapsed_component=Decimal("31.5"),
        layover_penalty=Decimal("10"),
    )
    second = _scored(
        _second_itinerary(),
        rank=2,
        fare_component=Decimal("780.00"),
        elapsed_component=Decimal("30.0"),
        layover_penalty=Decimal("0"),
    )
    return [top, second]


def _task_outcome(
    *,
    task_id: str,
    state: TaskState,
    error_type: str | None = None,
    error_detail: str | None = None,
    offer_count: int = 0,
    accepted_count: int = 0,
) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        state=state,
        attempts=1,
        duration_ms=100,
        offer_count=offer_count,
        accepted_count=accepted_count,
        cache="miss",
        error_type=error_type,
        error_detail=error_detail,
    )


def _ok_outcome(task_id: str = "AMS-DEL-s1") -> TaskOutcome:
    return _task_outcome(task_id=task_id, state=TaskState.OK, offer_count=2, accepted_count=2)


def _provider_error_outcome(task_id: str = "AMS-BOM-s1") -> TaskOutcome:
    return _task_outcome(
        task_id=task_id,
        state=TaskState.PROVIDER_ERROR,
        error_type="circuit_open",
        error_detail="Provider returned 503 for 10 consecutive attempts.",
    )


def _rate_limited_outcome(task_id: str = "AMS-BLR-s1") -> TaskOutcome:
    return _task_outcome(
        task_id=task_id,
        state=TaskState.RATE_LIMITED,
        error_type="rate_limited",
        error_detail="Retry-After exceeded the per-task retry budget.",
    )


class TestSyntheticDataBanner:
    """Master plan S8.6/S8.8 (CRITICAL): position, not just presence."""

    def test_banner_is_the_very_first_line(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )
        assert rendered.splitlines()[0] == SYNTHETIC_DATA_BANNER

    def test_banner_appears_before_every_other_section(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )
        banner_index = rendered.index(SYNTHETIC_DATA_BANNER)
        recommended_index = rendered.index("## Recommended Flight")
        other_options_index = rendered.index("## Other Good Options")

        assert banner_index == 0
        assert banner_index < recommended_index < other_options_index

    def test_banner_exact_wording(self) -> None:
        # Exact text, not just "contains the word synthetic" -- master plan
        # S8.6 quotes this precise sentence.
        assert SYNTHETIC_DATA_BANNER == (
            "**SYNTHETIC DATA — NOT REAL FARES — DO NOT BOOK BASED ON THIS REPORT**"
        )


class TestRecommendedFlightBlock:
    """Spec section 6: exactly these seven fields, no more, no fewer."""

    def _block(self) -> str:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )
        start = rendered.index("## Recommended Flight")
        end = rendered.index("## Other Good Options")
        return rendered[start:end]

    @pytest.mark.parametrize(
        "label",
        [
            "**Airline:**",
            "**Route:**",
            "**Departure:**",
            "**Layover:**",
            "**Arrival:**",
            "**Price (EUR):**",
            "**Booking:**",
        ],
    )
    def test_field_present_exactly_once(self, label: str) -> None:
        assert self._block().count(label) == 1

    def test_field_values_reflect_the_top_ranked_itinerary(self) -> None:
        block = self._block()
        assert "**Airline:** EK" in block
        assert "**Route:** AMS to DXB to DEL" in block
        assert "**Layover:** 4h 10m" in block
        assert "€725.00" in block
        assert "2027-07-17 09:00" in block  # 07:00 UTC + 2h CEST
        assert "(Europe/Amsterdam)" in block


class TestOtherGoodOptionsTable:
    """Spec section 6: exactly these four columns."""

    def test_exactly_four_required_column_headers(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )
        assert rendered.count("| Airline | Route | Layover | Price |") == 1

    def test_second_ranked_itinerary_is_a_row_not_the_recommended_flight(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )
        table = rendered[rendered.index("## Other Good Options") :]
        assert "TK" in table
        assert "€780.00" in table
        # No fifth "Booking" column -- spec section 6 lists exactly 4.
        assert "Booking" not in table

    def test_single_itinerary_has_no_other_options_table_at_all(self) -> None:
        solo = [_scored(_top_itinerary(), rank=1, fare_component=Decimal("725.00"))]
        rendered = render_markdown_report(
            solo, departure_date=_DEPARTURE_DATE, accepted_count=1, generated_at=_GENERATED_AT
        )
        assert "## Other Good Options" not in rendered


class TestEmptyInputRejected:
    def test_render_markdown_report_rejects_empty_ranked_list(self) -> None:
        with pytest.raises(ValueError, match="at least one ranked itinerary"):
            render_markdown_report(
                [], departure_date=_DEPARTURE_DATE, accepted_count=0, generated_at=_GENERATED_AT
            )


class TestBookingUrlValidator:
    """Master plan S8.2 v1 slice, exercised directly."""

    def test_accepts_https_mock_reserved_domain(self) -> None:
        result = validate_booking_url(_MOCK_BOOKING_URL, data_source="mock")
        assert result.url == _MOCK_BOOKING_URL
        assert result.host == "mock.flightagent.invalid"

    def test_rejects_non_https_scheme(self) -> None:
        with pytest.raises(BookingUrlRejected) as exc_info:
            validate_booking_url(_INSECURE_BOOKING_URL, data_source="mock")
        assert exc_info.value.reason == "non_https_scheme"

    def test_rejects_mock_url_on_a_non_reserved_domain(self) -> None:
        # https, otherwise well-formed -- but NOT a reserved test suffix,
        # so it must still be rejected: "no allowlist yet" must not mean
        # "anything https passes".
        with pytest.raises(BookingUrlRejected) as exc_info:
            validate_booking_url("https://real-airline-example.com/booking", data_source="mock")
        assert exc_info.value.reason == "mock_host_not_reserved"

    def test_rejects_userinfo_in_authority(self) -> None:
        with pytest.raises(BookingUrlRejected) as exc_info:
            validate_booking_url("https://user:pass@mock.flightagent.invalid/x", data_source="mock")
        assert exc_info.value.reason == "userinfo_present"

    def test_rejects_oversized_url(self) -> None:
        oversized = "https://mock.flightagent.invalid/" + ("a" * 3000)
        with pytest.raises(BookingUrlRejected) as exc_info:
            validate_booking_url(oversized, data_source="mock")
        assert exc_info.value.reason == "url_too_long"


class TestRenderedBookingLinkSafety:
    """The load-bearing property: an unsafe URL must never reach the
    rendered document as raw, clickable text -- not just that the
    validator, considered alone, would reject it."""

    def test_valid_mock_https_url_rendered_as_a_markdown_link(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )
        assert f"]({_MOCK_BOOKING_URL})" in rendered

    def test_non_https_booking_url_never_appears_raw_in_rendered_output(self) -> None:
        insecure = _top_itinerary(booking_url=_INSECURE_BOOKING_URL)
        scored = _scored(insecure, rank=1, fare_component=Decimal("900.00"))

        rendered = render_markdown_report(
            [scored], departure_date=_DEPARTURE_DATE, accepted_count=1, generated_at=_GENERATED_AT
        )

        assert "http://" not in rendered
        assert "insecure.example.com" not in rendered
        assert "booking link withheld" in rendered

    def test_missing_booking_url_renders_unavailable_not_a_broken_link(self) -> None:
        no_link = _one_stop_itinerary(
            itinerary_id="itin_nolink_0001",
            price_eur=Decimal("910.00"),
            carrier="AI",
            hub="DXB",
            hub_tz="Asia/Dubai",
            layover_minutes=220,
            booking_url=None,
            booking_url_kind="unavailable",
        )
        scored = _scored(no_link, rank=1, fare_component=Decimal("910.00"))

        rendered = render_markdown_report(
            [scored], departure_date=_DEPARTURE_DATE, accepted_count=1, generated_at=_GENERATED_AT
        )

        assert "no booking link available" in rendered
        assert "](" not in rendered.split("## Recommended Flight")[1]


class TestJsonArtifactDataSource:
    """Master plan S8.6 (CRITICAL): data_source at the TOP level, not
    buried inside a nested object."""

    def test_data_source_mock_is_a_top_level_key(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
        )
        assert doc["data_source"] == "mock"
        assert "data_source" in doc  # top-level key, not nested under e.g. "run_meta"

    def test_build_results_document_rejects_accepted_count_smaller_than_ranked_list(self) -> None:
        with pytest.raises(ValueError, match="accepted_count"):
            build_results_document(
                _ranked_pair(),
                departure_date=_DEPARTURE_DATE,
                accepted_count=1,  # smaller than len(_ranked_pair()) == 2
                top_n=10,
                generated_at=_GENERATED_AT,
            )


class TestJsonArtifactSchema:
    def test_document_validates_against_results_schema(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
        )
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(instance=doc, schema=schema)  # raises on failure

    def test_solo_itinerary_document_also_validates(self) -> None:
        solo = [_scored(_top_itinerary(), rank=1, fare_component=Decimal("725.00"))]
        doc = build_results_document(
            solo,
            departure_date=_DEPARTURE_DATE,
            accepted_count=1,
            top_n=10,
            generated_at=_GENERATED_AT,
        )
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(instance=doc, schema=schema)

    def test_document_with_invalid_booking_url_also_validates(self) -> None:
        insecure = _top_itinerary(booking_url=_INSECURE_BOOKING_URL)
        scored = _scored(insecure, rank=1, fare_component=Decimal("900.00"))
        doc = build_results_document(
            [scored],
            departure_date=_DEPARTURE_DATE,
            accepted_count=1,
            top_n=10,
            generated_at=_GENERATED_AT,
        )
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(instance=doc, schema=schema)


class TestJsonArtifactContent:
    def test_top_itinerary_carries_rank_and_exact_score_components(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
        )
        entry = doc["top_itineraries"][0]

        assert entry["rank_by_adjusted_score"] == 1
        assert entry["itinerary_id"] == "itin_top_0001"
        assert entry["price_eur"] == "725.00"
        assert entry["layover_minutes"] == 250
        assert entry["route"] == "AMS to DXB to DEL"

        expected_adjusted = Decimal("725.00") + Decimal("31.5") + Decimal("10")
        assert entry["score"]["adjusted_score"] == str(expected_adjusted)
        assert entry["score"]["fare_eur"] == str(Decimal("725.00"))

    def test_non_https_booking_url_marked_invalid_but_preserved_for_audit(self) -> None:
        insecure = _top_itinerary(booking_url=_INSECURE_BOOKING_URL)
        scored = _scored(insecure, rank=1, fare_component=Decimal("900.00"))
        doc = build_results_document(
            [scored],
            departure_date=_DEPARTURE_DATE,
            accepted_count=1,
            top_n=10,
            generated_at=_GENERATED_AT,
        )
        entry = doc["top_itineraries"][0]

        # Preserved (JSON is not a clickable rendering surface) but flagged.
        assert entry["booking_url"] == _INSECURE_BOOKING_URL
        assert entry["booking_url_valid"] is False


class TestFailedSearchesMarkdown:
    """Phase 4, T26: the "## Failed Searches" section."""

    def test_mixed_success_and_failure_renders_section_with_right_fields(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
            task_outcomes=[
                _ok_outcome(),
                _provider_error_outcome(),
                _rate_limited_outcome(),
            ],
        )

        assert "## Failed Searches" in rendered
        section = rendered[rendered.index("## Failed Searches") :]

        assert "| BOM | circuit_open | Provider returned 503 for 10 consecutive attempts. |" in (
            section
        )
        assert (
            "| BLR | rate_limited | Retry-After exceeded the per-task retry budget. |" in section
        )
        # The OK outcome's destination must never appear as a failure row.
        assert "| DEL |" not in section

    def test_nothing_failed_renders_no_failed_searches_heading_at_all(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
            task_outcomes=[_ok_outcome()],
        )

        assert "## Failed Searches" not in rendered
        assert "Failed Searches" not in rendered

    def test_default_task_outcomes_also_renders_no_failed_searches_heading(self) -> None:
        # No task_outcomes argument at all -- the pre-Phase-4 call shape.
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
        )

        assert "## Failed Searches" not in rendered

    def test_failed_searches_section_appears_after_other_good_options(self) -> None:
        rendered = render_markdown_report(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            generated_at=_GENERATED_AT,
            task_outcomes=[_provider_error_outcome()],
        )

        other_options_index = rendered.index("## Other Good Options")
        failed_index = rendered.index("## Failed Searches")
        summary_index = rendered.index("**Summary:**")

        assert other_options_index < failed_index < summary_index


class TestFailedSearchesJson:
    """Phase 4, T26: the top-level ``failed_searches`` array."""

    def test_mixed_success_and_failure_produces_exactly_the_error_state_entries(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
            task_outcomes=[
                _ok_outcome(),
                _provider_error_outcome(),
                _rate_limited_outcome(),
            ],
        )

        failed = doc["failed_searches"]
        assert len(failed) == 2  # not more, not fewer than the 2 error-state outcomes

        by_destination = {entry["destination"]: entry for entry in failed}
        assert set(by_destination) == {"BOM", "BLR"}

        assert by_destination["BOM"]["error_type"] == "circuit_open"
        assert by_destination["BOM"]["error_detail"] == (
            "Provider returned 503 for 10 consecutive attempts."
        )
        assert by_destination["BLR"]["error_type"] == "rate_limited"
        assert by_destination["BLR"]["error_detail"] == (
            "Retry-After exceeded the per-task retry budget."
        )

    def test_nothing_failed_yields_present_but_empty_array(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
            task_outcomes=[_ok_outcome()],
        )

        assert doc["failed_searches"] == []

    def test_default_task_outcomes_also_yields_present_but_empty_array(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
        )

        assert "failed_searches" in doc
        assert doc["failed_searches"] == []

    def test_non_error_terminal_states_never_appear_as_failures(self) -> None:
        # NO_OFFERS and ALL_REJECTED are legitimate empty results, not
        # failures (master plan 0.5) -- neither belongs in failed_searches.
        no_offers = _task_outcome(task_id="AMS-MST-s1", state=TaskState.NO_OFFERS)
        all_rejected = _task_outcome(task_id="AMS-GRQ-s1", state=TaskState.ALL_REJECTED)

        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
            task_outcomes=[no_offers, all_rejected, _provider_error_outcome()],
        )

        assert len(doc["failed_searches"]) == 1
        assert doc["failed_searches"][0]["destination"] == "BOM"

    def test_document_with_failed_searches_still_validates_against_schema(self) -> None:
        doc = build_results_document(
            _ranked_pair(),
            departure_date=_DEPARTURE_DATE,
            accepted_count=2,
            top_n=10,
            generated_at=_GENERATED_AT,
            task_outcomes=[_ok_outcome(), _provider_error_outcome()],
        )
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(instance=doc, schema=schema)


class TestAtomicWriter:
    def test_write_report_artifacts_creates_both_files_with_full_content(
        self, tmp_path: Path
    ) -> None:
        report_path = tmp_path / "flight_report_2027-07-17.md"
        results_path = tmp_path / "flight_results_2027-07-17.json"
        markdown = "hello synthetic report\n"
        data = {"data_source": "mock", "n": 1}

        returned_report, returned_results = write_report_artifacts(
            markdown=markdown,
            json_data=data,
            report_path=report_path,
            results_path=results_path,
        )

        assert returned_report == report_path
        assert returned_results == results_path
        assert report_path.read_text(encoding="utf-8") == markdown
        assert json.loads(results_path.read_text(encoding="utf-8")) == data
        # No leftover temp files -- the writer's own naming convention
        # (".{name}.<random>.tmp") must never collide with, or survive
        # alongside, the final file.
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob(".*")) == []

    def test_atomic_write_replaces_existing_content_wholly_not_partially(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "report.md"
        path.write_text("OLD CONTENT THAT MUST NOT SURVIVE THE REWRITE", encoding="utf-8")

        atomic_write_text(path, "brand new content")

        assert path.read_text(encoding="utf-8") == "brand new content"

    def test_creates_missing_parent_directory(self, tmp_path: Path) -> None:
        nested_path = tmp_path / "out" / "report.md"

        atomic_write_text(nested_path, "content")

        assert nested_path.read_text(encoding="utf-8") == "content"

    def test_interrupted_write_leaves_no_partial_or_final_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a crash between "write" and "rename": if the write
        step itself fails, the destination path must never come into
        existence, and the temp file must be cleaned up rather than left
        behind as a stray partial artifact."""
        path = tmp_path / "report.md"

        def _boom(_fd: int) -> None:
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(os, "fsync", _boom)

        with pytest.raises(OSError, match="simulated crash mid-write"):
            atomic_write_text(path, "content that must never land")

        assert not path.exists()
        assert list(tmp_path.iterdir()) == []
