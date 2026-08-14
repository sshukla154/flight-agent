"""Domain model smoke tests (Phase 1 / T7).

Not the full Phase 3 validation suite — just enough to catch a broken
model now rather than three phases later. The DST fall-back assertion
(``test_layover_across_dst_fallback_is_210_minutes_in_utc``) is the single
most important test in this file: it is the entire reason the
``depart_fold``/``arrive_fold``/``ambiguous_local_time`` fields exist on
``Segment``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter, ValidationError

from flightagent.domain.airport import IataCode
from flightagent.domain.enums import CabinClass
from flightagent.domain.ids import compute_itinerary_id
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.scoring import ScoreComponents, ScoredItinerary
from flightagent.domain.segment import Layover, Segment


def _make_segment(
    *,
    origin: str = "AMS",
    destination: str = "DXB",
    depart_utc: datetime = datetime(2027, 7, 17, 10, 0, tzinfo=UTC),
    arrive_utc: datetime = datetime(2027, 7, 17, 18, 0, tzinfo=UTC),
    marketing_carrier: str = "KL",
    flight_number: str = "429",
) -> Segment:
    """AMS (Europe/Amsterdam, CEST=+2) -> DXB (Asia/Dubai, +4) in July, so
    depart_local/arrive_local below are always the correct offsets for the
    default UTC instants; callers overriding depart_utc/arrive_utc must
    keep that in mind (only used with defaults in these tests)."""
    return Segment(
        segment_id=f"{origin}-{destination}-{flight_number}",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(ZoneInfo("Europe/Amsterdam")),
        arrive_local=arrive_utc.astimezone(ZoneInfo("Asia/Dubai")),
        origin_tz="Europe/Amsterdam",
        destination_tz="Asia/Dubai",
        marketing_carrier=marketing_carrier,
        flight_number=flight_number,
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _shape_key_from_segments(segments: list[Segment]) -> str:
    """A stand-in for the real dedup shape-key builder (normalize/dedup.py,
    out of scope here) — good enough to exercise ids.compute_itinerary_id's
    own determinism contract, which is all this test file cares about."""
    return str(
        tuple(
            (s.origin, s.destination, s.depart_utc.isoformat(), s.arrive_utc.isoformat())
            for s in segments
        )
    )


class TestFrozenModel:
    def test_frozen_model_raises_on_mutation(self) -> None:
        money = Money(amount=Decimal("10.00"), currency="EUR")
        with pytest.raises(ValidationError):
            money.amount = Decimal("20.00")  # type: ignore[misc]


class TestSegmentNaiveDatetime:
    def test_segment_rejects_naive_depart_utc(self) -> None:
        with pytest.raises(ValidationError):
            Segment(
                segment_id="seg-naive",
                origin="AMS",
                destination="DEL",
                depart_utc=datetime(2027, 7, 17, 10, 0),  # naive — no tzinfo
                arrive_utc=datetime(2027, 7, 17, 19, 0, tzinfo=UTC),
                depart_local=datetime(2027, 7, 17, 12, 0, tzinfo=ZoneInfo("Europe/Amsterdam")),
                arrive_local=datetime(2027, 7, 18, 0, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
                origin_tz="Europe/Amsterdam",
                destination_tz="Asia/Kolkata",
                marketing_carrier="KL",
                flight_number="872",
                cabin=CabinClass.ECONOMY,
                duration=timedelta(hours=9),
            )


class TestItineraryIdDeterminism:
    def test_itinerary_id_is_deterministic_across_identical_segments(self) -> None:
        price = Money(amount=Decimal("650.00"), currency="EUR")

        seg_a = _make_segment()
        seg_b = _make_segment()  # independently constructed, identical field values
        assert seg_a == seg_b

        id_from_a = compute_itinerary_id(
            _shape_key_from_segments([seg_a]), [seg_a.marketing_carrier], price
        )
        id_from_b = compute_itinerary_id(
            _shape_key_from_segments([seg_b]), [seg_b.marketing_carrier], price
        )
        assert id_from_a == id_from_b

    def test_itinerary_id_is_independent_of_carrier_iteration_order(self) -> None:
        price = Money(amount=Decimal("650.00"), currency="EUR")
        shape_key = _shape_key_from_segments([_make_segment()])

        forward = compute_itinerary_id(shape_key, ["KL", "AF"], price)
        reversed_order = compute_itinerary_id(shape_key, ["AF", "KL"], price)
        assert forward == reversed_order

    def test_itinerary_id_changes_when_a_field_changes(self) -> None:
        price = Money(amount=Decimal("650.00"), currency="EUR")
        shape_key = _shape_key_from_segments([_make_segment()])
        baseline = compute_itinerary_id(shape_key, ["KL"], price)

        different_price = Money(amount=Decimal("650.01"), currency="EUR")
        assert compute_itinerary_id(shape_key, ["KL"], different_price) != baseline

        different_shape_key = _shape_key_from_segments([_make_segment(destination="DEL")])
        assert compute_itinerary_id(different_shape_key, ["KL"], price) != baseline

        assert compute_itinerary_id(shape_key, ["AF"], price) != baseline


class TestMoney:
    def test_money_quantizes_10_005_to_10_01_under_round_half_up(self) -> None:
        money = Money(amount=Decimal("10.005"), currency="EUR")
        # Decimal("10.005") is an EXACT value (unlike the float 10.005), so
        # ROUND_HALF_UP has exactly one correct answer here, not a choice.
        assert money.amount == Decimal("10.01")

    def test_money_rejects_float_input(self) -> None:
        with pytest.raises(ValidationError):
            Money(amount=10.005, currency="EUR")  # type: ignore[arg-type]


class TestIataCode:
    def test_iata_code_accepts_valid_code(self) -> None:
        adapter = TypeAdapter(IataCode)
        assert adapter.validate_python("AMS") == "AMS"

    def test_iata_code_rejects_lowercase(self) -> None:
        adapter = TypeAdapter(IataCode)
        with pytest.raises(ValidationError):
            adapter.validate_python("ams")

    def test_iata_code_rejects_four_letters(self) -> None:
        adapter = TypeAdapter(IataCode)
        with pytest.raises(ValidationError):
            adapter.validate_python("AMST")


class TestDstFallbackLayover:
    def test_layover_across_dst_fallback_is_210_minutes_in_utc(self) -> None:
        """spikes/tz_arithmetic.py case_1: Europe/Amsterdam, 2027-10-31.

        Inbound arrives 01:00 CEST (unambiguous — the repeated hour that
        year is 02:00-02:59, not 01:00). Outbound departs 03:30 CET
        (likewise unambiguous, already past the repeated hour). Local
        clock reads a 2h30m gap; the true UTC-elapsed gap is 3h30m = 210
        minutes. A naive local-clock subtraction gives the wrong answer
        (150 minutes) while looking entirely plausible — this is the exact
        bug the tz spike exists to prevent, and the reason Layover.duration
        must be computed exclusively in UTC.
        """
        amsterdam = ZoneInfo("Europe/Amsterdam")
        brussels = ZoneInfo("Europe/Brussels")
        dubai = ZoneInfo("Asia/Dubai")

        inbound_depart_utc = datetime(2027, 10, 30, 16, 0, tzinfo=UTC)
        inbound_arrive_utc = datetime(2027, 10, 30, 23, 0, tzinfo=UTC)  # 01:00 CEST on 10-31
        inbound = Segment(
            segment_id="DXB-AMS-148",
            origin="DXB",
            destination="AMS",
            depart_utc=inbound_depart_utc,
            arrive_utc=inbound_arrive_utc,
            depart_local=inbound_depart_utc.astimezone(dubai),
            arrive_local=inbound_arrive_utc.astimezone(amsterdam),
            origin_tz="Asia/Dubai",
            destination_tz="Europe/Amsterdam",
            marketing_carrier="EK",
            flight_number="148",
            cabin=CabinClass.ECONOMY,
            duration=inbound_arrive_utc - inbound_depart_utc,
            arrive_fold=0,
        )
        assert not inbound.ambiguous_local_time, "01:00 local on 2027-10-31 is unambiguous CEST"

        outbound_depart_utc = datetime(2027, 10, 31, 2, 30, tzinfo=UTC)  # 03:30 CET
        outbound_arrive_utc = datetime(2027, 10, 31, 3, 30, tzinfo=UTC)  # 04:30 CET
        outbound = Segment(
            segment_id="AMS-BRU-512",
            origin="AMS",
            destination="BRU",
            depart_utc=outbound_depart_utc,
            arrive_utc=outbound_arrive_utc,
            depart_local=outbound_depart_utc.astimezone(amsterdam),
            arrive_local=outbound_arrive_utc.astimezone(brussels),
            origin_tz="Europe/Amsterdam",
            destination_tz="Europe/Brussels",
            marketing_carrier="EK",
            flight_number="512",
            cabin=CabinClass.ECONOMY,
            duration=outbound_arrive_utc - outbound_depart_utc,
            depart_fold=0,
        )
        assert not outbound.ambiguous_local_time, "03:30 local on 2027-10-31 is unambiguous CET"

        naive_local_minutes = (
            outbound.depart_local.replace(tzinfo=None) - inbound.arrive_local.replace(tzinfo=None)
        ).total_seconds() / 60
        assert naive_local_minutes == 150, (
            "sanity: local wall-clock subtraction gives the WRONG answer"
        )

        layover = Layover(
            airport="AMS",
            arrive_utc=inbound.arrive_utc,
            depart_utc=outbound.depart_utc,
            duration=outbound.depart_utc - inbound.arrive_utc,
            local_window=(inbound.arrive_local, outbound.depart_local),
            requires_airport_change=False,
            requires_terminal_change=False,
        )

        assert layover.duration == timedelta(minutes=210)
        assert layover.duration.total_seconds() / 60 == 210


class TestScoredItineraryTiebreak:
    def test_tiebreak_key_ends_with_itinerary_id(self) -> None:
        segment = _make_segment()
        leg = Leg(segments=(segment,), layovers=())
        price = Money(amount=Decimal("650.00"), currency="EUR")
        itinerary = NormalizedItinerary(
            itinerary_id="itin_test_0001",
            provider="mock",
            legs=(leg,),
            price_original=price,
            price_eur=price,
            booking_url_kind="unavailable",
            shape_key=_shape_key_from_segments([segment]),
            fare_as_of=datetime(2027, 1, 1, tzinfo=UTC),
        )
        components = ScoreComponents(
            fare_eur=Decimal("650.00"),
            elapsed_time_component=Decimal("24.00"),
            layover_penalty=Decimal("0.00"),
            direct_bonus=Decimal("-120.00"),
        )
        scored = ScoredItinerary(
            itinerary=itinerary,
            components=components,
            ground=None,
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )

        assert scored.tiebreak_key[-1] == itinerary.itinerary_id
        assert scored.tiebreak_key[-1] == "itin_test_0001"
