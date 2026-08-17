"""Unit tests for T38: the D7 ground-travel hard filter (plan-time, per
origin) and the parallel ``total_journey_score`` overlay.

Two modules under test:

- ``flightagent.orchestration.ground_filter`` — the 150-minute plan-time
  filter (master plan S6): "any origin whose GroundLeg.duration exceeds
  max_ground_travel_minutes is excluded from planning entirely", evaluated
  per origin, never per itinerary.
- ``flightagent.scoring.ground`` — ``ground_cost_component``/
  ``ground_time_component`` and the ``door_to_door_hours`` derived value,
  which feed ``domain.scoring.ScoredItinerary.total_journey_score``
  (already built in Phase 1 as a computed field).

Finding 0.3: every numeric assertion below is against an exact ``Decimal``
value, hand-computed and shown inline — never an approximate/float
comparison.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from flightagent.airports import registry
from flightagent.airports.registry import Airport
from flightagent.config.loader import load_config
from flightagent.domain.enums import CabinClass, RejectionCode
from flightagent.domain.ground import GroundLeg
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.scoring import ScoredItinerary
from flightagent.domain.segment import Segment
from flightagent.orchestration.ground_filter import (
    filter_origins_by_ground_limit,
    plannable_origins,
)
from flightagent.scoring.ground import (
    apply_ground_overlay,
    door_to_door_hours,
    ground_cost_component,
    ground_time_component,
)
from flightagent.scoring.score import score_itinerary

_SETTINGS = load_config(env={})

_EXPECTED_ORIGIN_ORDER = ["AMS", "EIN", "RTM", "DUS", "BRU", "NRN", "CGN", "CRL", "MST", "GRQ"]


def _synthetic_origin(iata: str, *, ground_minutes: int, priority: int = 99) -> Airport:
    """A wholly synthetic origin Airport, for the "an eleventh airport
    exceeds the limit" case master plan S6 itself names as the reason this
    filter must exist even though it never fires against today's real
    10-origin roster.
    """
    return Airport(
        iata=iata,
        name=f"Synthetic {iata}",
        city="Nowhere",
        country="Testland",
        iana_tz="UTC",
        lat=0.0,
        lon=0.0,
        priority=priority,
        ground=GroundLeg(
            from_location="Nieuwegein, Utrecht, NL",
            to_airport=iata,
            mode="car",
            duration=timedelta(minutes=ground_minutes),
            distance_km=Decimal("300"),
            cost=Money(amount=Decimal("60.00"), currency="EUR"),
            source="estimate",
            as_of=date(2026, 8, 14),
        ),
    )


class TestGroundTravelFilterRealRoster:
    """A real, non-contrived check against the actual registry data."""

    def test_all_ten_real_origins_pass_the_default_150_minute_filter(self) -> None:
        origins = registry.origins()
        assert [a.iata for a in origins] == _EXPECTED_ORIGIN_ORDER

        plannable, rejections = filter_origins_by_ground_limit(
            origins, max_ground_travel_minutes=150
        )

        assert [a.iata for a in plannable] == _EXPECTED_ORIGIN_ORDER
        assert rejections == []

    def test_plannable_origins_against_real_config_excludes_nothing(self) -> None:
        assert _SETTINGS.ground_travel.max_ground_travel_minutes == 150

        plannable, rejections = plannable_origins(settings=_SETTINGS)

        assert [a.iata for a in plannable] == _EXPECTED_ORIGIN_ORDER
        assert rejections == []


class TestGroundTravelFilterOverriddenThreshold:
    """"someone will add an eleventh airport" (master plan S6) — proven here
    with an overridden threshold against the REAL registry data, so only
    AMS (45 min) survives and every other real origin is correctly
    excluded.
    """

    def test_overridden_threshold_excludes_nine_of_ten_real_origins(self) -> None:
        plannable, rejections = filter_origins_by_ground_limit(
            registry.origins(), max_ground_travel_minutes=50
        )

        assert [a.iata for a in plannable] == ["AMS"]
        excluded_iatas = {a.iata for a in registry.origins()} - {"AMS"}
        assert excluded_iatas == {
            "EIN",
            "RTM",
            "DUS",
            "BRU",
            "NRN",
            "CGN",
            "CRL",
            "MST",
            "GRQ",
        }
        # Every excluded origin's real ground minutes, keyed by IATA — proves the
        # Rejection carries the correct per-origin observed value, not just a count.
        expected_observed_by_iata = {
            "EIN": "65",
            "RTM": "60",
            "DUS": "90",
            "BRU": "120",
            "NRN": "80",
            "CGN": "120",
            "CRL": "140",
            "MST": "120",
            "GRQ": "130",
        }
        rejections_by_message_iata = {
            iata: rejection
            for iata in expected_observed_by_iata
            for rejection in rejections
            if rejection.message.startswith(iata)
        }
        assert set(rejections_by_message_iata) == set(expected_observed_by_iata)
        for iata, expected_observed in expected_observed_by_iata.items():
            assert rejections_by_message_iata[iata].observed == expected_observed
        assert len(rejections) == 9
        for rejection in rejections:
            assert rejection.code == RejectionCode.GROUND_TRAVEL_EXCEEDED
            assert rejection.rule_id == "ground_travel_limit"
            assert rejection.expected == "<= 50"

    def test_plannable_origins_honours_an_overridden_settings_threshold(self) -> None:
        overridden = load_config(
            env={"FLIGHTAGENT__GROUND_TRAVEL__MAX_GROUND_TRAVEL_MINUTES": "50"}
        )
        assert overridden.ground_travel.max_ground_travel_minutes == 50

        plannable, rejections = plannable_origins(settings=overridden)

        assert [a.iata for a in plannable] == ["AMS"]
        assert len(rejections) == 9


class TestGroundTravelFilterSyntheticOrigin:
    def test_synthetic_eleventh_origin_over_limit_is_excluded(self) -> None:
        eleventh = _synthetic_origin("ZZZ", ground_minutes=200)

        plannable, rejections = filter_origins_by_ground_limit(
            [*registry.origins(), eleventh], max_ground_travel_minutes=150
        )

        assert [a.iata for a in plannable] == _EXPECTED_ORIGIN_ORDER
        assert len(rejections) == 1
        rejection = rejections[0]
        assert rejection.code == RejectionCode.GROUND_TRAVEL_EXCEEDED
        assert "ZZZ" in rejection.message
        assert "200" in rejection.message
        assert rejection.observed == "200"
        assert rejection.expected == "<= 150"
        assert rejection.rule_id == "ground_travel_limit"

    def test_synthetic_origin_exactly_at_the_limit_is_not_excluded(self) -> None:
        """Master plan S6 says "exceeds" — exactly 150 minutes must still
        pass, not be treated as over the line."""
        at_limit = _synthetic_origin("YYY", ground_minutes=150)

        plannable, rejections = filter_origins_by_ground_limit(
            [at_limit], max_ground_travel_minutes=150
        )

        assert [a.iata for a in plannable] == ["YYY"]
        assert rejections == []

    def test_synthetic_origin_one_minute_over_the_limit_is_excluded(self) -> None:
        just_over = _synthetic_origin("XXX", ground_minutes=151)

        plannable, rejections = filter_origins_by_ground_limit(
            [just_over], max_ground_travel_minutes=150
        )

        assert plannable == []
        assert len(rejections) == 1
        assert rejections[0].observed == "151"

    def test_filter_raises_for_an_airport_with_no_ground_leg(self) -> None:
        destination = registry.destinations()[0]
        assert destination.ground is None

        with pytest.raises(ValueError, match="no ground-access leg"):
            filter_origins_by_ground_limit([destination], max_ground_travel_minutes=150)


class TestGroundScoreComponents:
    """``ground_cost_component``/``ground_time_component`` against a
    hand-picked real origin's real ``GroundLeg`` (AMS), with the exact
    expected ``Decimal`` values shown inline.
    """

    def test_ams_ground_leg_has_the_expected_real_data(self) -> None:
        """Precondition, not the thing under test: if this ever fails,
        config/ground_access.yaml changed and the hand-computed values
        below need updating too — better a loud failure here than a
        silently-wrong "expected" constant."""
        ams = registry.get("AMS")
        assert ams.ground is not None
        assert ams.ground.duration == timedelta(minutes=45)
        assert ams.ground.cost.amount == Decimal("12.00")

    def test_ground_cost_component_matches_hand_computed_value(self) -> None:
        ams = registry.get("AMS")
        assert ams.ground is not None

        result = ground_cost_component(
            ams.ground, ground_cost_weight=_SETTINGS.ground_travel.ground_cost_weight
        )

        # weight 1.0 * EUR 12.00 = 12.000
        assert result == Decimal("12.000")
        assert isinstance(result, Decimal)
        assert not isinstance(result, float)

    def test_ground_time_component_matches_hand_computed_value(self) -> None:
        ams = registry.get("AMS")
        assert ams.ground is not None

        result = ground_time_component(
            ams.ground, ground_time_weight=_SETTINGS.ground_travel.ground_time_weight
        )

        # weight 8.0 * (45 min = 0.75h) = 6.000
        assert result == Decimal("6.000")
        assert isinstance(result, Decimal)
        assert not isinstance(result, float)

    def test_components_scale_with_a_different_real_origin(self) -> None:
        """CRL (140 min, EUR 35.00) — proves the functions genuinely read
        the given ``GroundLeg``, not a hardcoded AMS constant."""
        crl = registry.get("CRL")
        assert crl.ground is not None
        assert crl.ground.duration == timedelta(minutes=140)
        assert crl.ground.cost.amount == Decimal("35.00")

        cost = ground_cost_component(
            crl.ground, ground_cost_weight=_SETTINGS.ground_travel.ground_cost_weight
        )
        time_ = ground_time_component(
            crl.ground, ground_time_weight=_SETTINGS.ground_travel.ground_time_weight
        )

        # 1.0 * 35.00 = 35.000 ; 8.0 * (140/60 h) = 8.0 * 2.333... -> Decimal division,
        # not a round number, but must still be exact Decimal arithmetic.
        assert cost == Decimal("35.000")
        expected_hours = Decimal(140) / Decimal(60)
        assert time_ == _SETTINGS.ground_travel.ground_time_weight * expected_hours


def _segment(
    *,
    origin: str,
    destination: str,
    depart_utc: datetime,
    arrive_utc: datetime,
    flight_number: str,
) -> Segment:
    zone = ZoneInfo("UTC")
    return Segment(
        segment_id=f"{origin}-{destination}-{flight_number}",
        origin=origin,
        destination=destination,
        depart_utc=depart_utc,
        arrive_utc=arrive_utc,
        depart_local=depart_utc.astimezone(zone),
        arrive_local=arrive_utc.astimezone(zone),
        origin_tz="UTC",
        destination_tz="UTC",
        marketing_carrier="KL",
        flight_number=flight_number,
        cabin=CabinClass.ECONOMY,
        duration=arrive_utc - depart_utc,
    )


def _direct_ams_del_itinerary() -> NormalizedItinerary:
    """AMS(08:00Z) -> DEL(16:00Z), 8h, direct, fare EUR 900.00 — the same
    shape ``test_scorer.py``'s own ``_direct_itinerary`` uses, duplicated
    here (not imported: that helper is test-module-private) so this
    worked example's numbers are self-contained and traceable in this
    file alone.
    """
    seg = _segment(
        origin="AMS",
        destination="DEL",
        depart_utc=datetime(2027, 7, 17, 8, 0, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 16, 0, tzinfo=UTC),
        flight_number="872",
    )
    leg = Leg(segments=(seg,), layovers=())
    price = Money(amount=Decimal("900.00"), currency="EUR")
    return NormalizedItinerary(
        itinerary_id="itin_ground_overlay_0001",
        provider="mock",
        legs=(leg,),
        price_original=price,
        price_eur=price,
        booking_url_kind="unavailable",
        shape_key="shape-itin_ground_overlay_0001",
        fare_as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )


class TestTotalJourneyScoreWorkedExample:
    """total_journey_score = adjusted_score + ground_cost_component +
    ground_time_component (D7) — verified against a concrete worked
    example built from real AMS ground data and real default config.

    By hand:
      fare_eur=900.00, elapsed_time_component=8h*3.0=24.0, layover_penalty=0
      score = 924.00 ; direct_bonus = -120.0 (fixed, direct itinerary)
      adjusted_score = 804.00
      ground_cost_component = 1.0 * 12.00 = 12.000
      ground_time_component = 8.0 * 0.75h = 6.000
      total_journey_score = 804.00 + 12.000 + 6.000 = 822.000
    """

    def test_ground_is_zero_before_the_overlay_is_applied(self) -> None:
        itinerary = _direct_ams_del_itinerary()
        components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        assert components.adjusted_score == Decimal("804.00")

        scored = ScoredItinerary(
            itinerary=itinerary,
            components=components,
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )

        assert scored.ground is None
        assert scored.ground_cost_component == Decimal(0)
        assert scored.ground_time_component == Decimal(0)
        # No ground overlay yet -> total_journey_score reduces to adjusted_score.
        assert scored.total_journey_score == components.adjusted_score

    def test_total_journey_score_matches_hand_computed_value_after_overlay(self) -> None:
        itinerary = _direct_ams_del_itinerary()
        components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        scored = ScoredItinerary(
            itinerary=itinerary,
            components=components,
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )

        ams = registry.get("AMS")
        assert ams.ground is not None

        overlaid = apply_ground_overlay(
            scored, ground_leg=ams.ground, settings=_SETTINGS.ground_travel
        )

        assert overlaid.ground == ams.ground
        assert overlaid.ground_cost_component == Decimal("12.000")
        assert overlaid.ground_time_component == Decimal("6.000")
        assert overlaid.total_journey_score == Decimal("822.000")

        # apply_ground_overlay never mutates its input (frozen model, new instance).
        assert scored.ground is None
        assert scored.total_journey_score == Decimal("804.00")

    def test_all_total_journey_score_inputs_are_decimal_never_float(self) -> None:
        itinerary = _direct_ams_del_itinerary()
        components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        scored = ScoredItinerary(
            itinerary=itinerary,
            components=components,
            rank_by_adjusted_score=1,
            rank_by_total_journey_score=1,
            rank_by_price=1,
        )
        ams = registry.get("AMS")
        assert ams.ground is not None

        overlaid = apply_ground_overlay(
            scored, ground_leg=ams.ground, settings=_SETTINGS.ground_travel
        )

        for value in (
            overlaid.ground_cost_component,
            overlaid.ground_time_component,
            overlaid.total_journey_score,
        ):
            assert isinstance(value, Decimal)
            assert not isinstance(value, float)


class TestDoorToDoorHours:
    """flight total_duration + ground_leg.duration, summed in Decimal
    hours — the origin-comparison-table value T41 will surface.
    """

    def test_door_to_door_hours_sums_flight_and_ground_duration(self) -> None:
        itinerary = _direct_ams_del_itinerary()
        assert itinerary.total_duration == timedelta(hours=8)

        ams = registry.get("AMS")
        assert ams.ground is not None
        assert ams.ground.duration == timedelta(minutes=45)

        result = door_to_door_hours(itinerary.total_duration, ams.ground)

        # 8h flight + 45min ground = 8h45m = 8.75h exactly.
        assert result == Decimal("8.75")
        assert isinstance(result, Decimal)
        assert not isinstance(result, float)

    def test_door_to_door_hours_with_zero_ground_duration_equals_flight_duration_alone(
        self,
    ) -> None:
        zero_ground = GroundLeg(
            from_location="Nieuwegein, Utrecht, NL",
            to_airport="AMS",
            mode="car",
            duration=timedelta(0),
            distance_km=Decimal("0"),
            cost=Money(amount=Decimal("0.00"), currency="EUR"),
            source="estimate",
            as_of=date(2026, 8, 14),
        )
        result = door_to_door_hours(timedelta(hours=8), zero_ground)
        assert result == Decimal("8")
