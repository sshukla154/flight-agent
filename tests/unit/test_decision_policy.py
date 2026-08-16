"""Unit tests for flightagent.policy.direct_vs_stop (Phase 5, T31, D10).

Master plan finding 0.1 / DECISIONS.md D10 (CONFIRMED 2026-08-16): the
direct-vs-one-stop tier ladder is ``RECOMMENDED`` if ``diff <= 100`` or
``rel <= 0.10``; ``GOOD_VALUE`` if ``diff <= 150`` or ``rel <= 0.20``;
``NOT_RECOMMENDED`` otherwise; ``NOT_AVAILABLE`` if no direct service
exists. ``TestThreeWorkedSpecCases`` proves the three cases DECISIONS.md's
own confirmation names exactly.

Finding 0.1's own worked example (stop EUR1000, direct EUR1195: policy says
"recommend direct" via the relative arm, but ``adjusted_score`` ranks the
one-stop first) is reproduced by hand in ``TestScorePolicyDivergence`` --
see that class's docstring for the arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flightagent.config.loader import load_config
from flightagent.config.models import DirectTierSettings
from flightagent.domain.enums import CabinClass, DirectTier
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.policy import DestinationAnalysis
from flightagent.domain.segment import Layover, Segment
from flightagent.policy.direct_vs_stop import analyze_destination, evaluate_direct_tier

_SETTINGS = load_config(env={})
_DEPARTURE = datetime(2027, 7, 17, 0, 0, tzinfo=UTC)


def _segment(
    *,
    origin: str,
    destination: str,
    depart_utc: datetime,
    arrive_utc: datetime,
    flight_number: str,
) -> Segment:
    """UTC-zoned on both ends, mirroring test_scorer.py's helper -- this file
    only cares about price/score arithmetic, not tz correctness."""
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


def _layover(*, airport: str, arrive_utc: datetime, depart_utc: datetime) -> Layover:
    zone = ZoneInfo("UTC")
    return Layover(
        airport=airport,
        arrive_utc=arrive_utc,
        depart_utc=depart_utc,
        duration=depart_utc - arrive_utc,
        local_window=(arrive_utc.astimezone(zone), depart_utc.astimezone(zone)),
        requires_airport_change=False,
        requires_terminal_change=False,
    )


def _itinerary(
    *, legs: tuple[Leg, ...], price_eur: Decimal, itinerary_id: str
) -> NormalizedItinerary:
    price = Money(amount=price_eur, currency="EUR")
    return NormalizedItinerary(
        itinerary_id=itinerary_id,
        provider="mock",
        legs=legs,
        price_original=price,
        price_eur=price,
        booking_url_kind="unavailable",
        shape_key=f"shape-{itinerary_id}",
        fare_as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _direct_itinerary(
    price_eur: Decimal,
    *,
    itinerary_id: str,
    duration: timedelta = timedelta(hours=8),
) -> NormalizedItinerary:
    """A single-segment, zero-layover itinerary -- stop_count == 0."""
    arrive = _DEPARTURE + duration
    seg = _segment(
        origin="AMS", destination="DEL", depart_utc=_DEPARTURE, arrive_utc=arrive, flight_number="1"
    )
    leg = Leg(segments=(seg,), layovers=())
    assert leg.connection_count == 0
    return _itinerary(legs=(leg,), price_eur=price_eur, itinerary_id=itinerary_id)


def _one_stop_itinerary(
    price_eur: Decimal,
    *,
    itinerary_id: str,
    first_leg: timedelta,
    layover_duration: timedelta,
    second_leg: timedelta,
) -> NormalizedItinerary:
    """A two-segment, one-layover itinerary -- stop_count == 1."""
    arrive1 = _DEPARTURE + first_leg
    depart2 = arrive1 + layover_duration
    arrive2 = depart2 + second_leg
    seg1 = _segment(
        origin="AMS",
        destination="DXB",
        depart_utc=_DEPARTURE,
        arrive_utc=arrive1,
        flight_number="1",
    )
    layover = _layover(airport="DXB", arrive_utc=arrive1, depart_utc=depart2)
    seg2 = _segment(
        origin="DXB", destination="DEL", depart_utc=depart2, arrive_utc=arrive2, flight_number="2"
    )
    leg = Leg(segments=(seg1, seg2), layovers=(layover,))
    itinerary = _itinerary(legs=(leg,), price_eur=price_eur, itinerary_id=itinerary_id)
    assert itinerary.stop_count == 1
    return itinerary


def _analyze(
    *,
    destination: str = "DEL",
    direct_pool: tuple[NormalizedItinerary, ...] = (),
    one_stop_pool: tuple[NormalizedItinerary, ...] = (),
    direct_tier_settings: DirectTierSettings | None = None,
) -> DestinationAnalysis:
    return analyze_destination(
        destination,
        direct_pool=direct_pool,
        one_stop_pool=one_stop_pool,
        direct_tier_settings=direct_tier_settings or _SETTINGS.direct_tier,
        scoring_settings=_SETTINGS.scoring,
        layover_settings=_SETTINGS.layover,
    )


# The master plan's own "worked example" one-stop shape: 13h30m total, a
# 210min (3h30m) layover -- band [180,240) -> 0 penalty. Reused across
# several tests below so the same non-divergent baseline keeps recurring.
_WORKED_STOP_FIRST_LEG = timedelta(hours=6)
_WORKED_STOP_LAYOVER = timedelta(minutes=210)
_WORKED_STOP_SECOND_LEG = timedelta(hours=4)


class TestThreeWorkedSpecCases:
    """DECISIONS.md D10's own three confirmed cases, exactly."""

    def test_case_1_diff_90_rel_14_5_percent_is_recommended(self) -> None:
        direct = _direct_itinerary(Decimal("710.00"), itinerary_id="itin_direct_1")
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_stop_1",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        expected_diff = Decimal("710.00") - Decimal("620.00")
        assert analysis.tier == DirectTier.RECOMMENDED
        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == expected_diff == Decimal("90.00")
        assert analysis.relative_difference == expected_diff / Decimal("620.00")

    def test_case_2_diff_110_rel_18_97_percent_is_good_value(self) -> None:
        direct = _direct_itinerary(Decimal("690.00"), itinerary_id="itin_direct_2")
        stop = _one_stop_itinerary(
            Decimal("580.00"),
            itinerary_id="itin_stop_2",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        expected_diff = Decimal("690.00") - Decimal("580.00")
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == expected_diff == Decimal("110.00")
        assert analysis.relative_difference == expected_diff / Decimal("580.00")

    def test_case_3_diff_300_rel_53_6_percent_is_not_recommended(self) -> None:
        direct = _direct_itinerary(Decimal("860.00"), itinerary_id="itin_direct_3")
        stop = _one_stop_itinerary(
            Decimal("560.00"),
            itinerary_id="itin_stop_3",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        expected_diff = Decimal("860.00") - Decimal("560.00")
        assert analysis.tier == DirectTier.NOT_RECOMMENDED
        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == expected_diff == Decimal("300.00")
        assert analysis.relative_difference == expected_diff / Decimal("560.00")


class TestOrSemanticsRulesCanDisagree:
    """Each tier's predicate is OR, not AND: either arm alone is sufficient,
    and ``tier_reason`` must name which one actually fired."""

    def test_recommended_absolute_arm_fires_relative_arm_fails(self) -> None:
        # diff = 100 (<=100, passes), rel = 100/500 = 0.20 (>0.10, fails).
        direct = _direct_itinerary(Decimal("600.00"), itinerary_id="itin_or_1d")
        stop = _one_stop_itinerary(
            Decimal("500.00"),
            itinerary_id="itin_or_1s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))
        assert analysis.tier == DirectTier.RECOMMENDED
        assert "absolute-difference rule fired" in analysis.tier_reason
        assert "relative-difference rule fired" not in analysis.tier_reason

    def test_recommended_relative_arm_fires_absolute_arm_fails(self) -> None:
        # diff = 105 (>100, fails), rel = 105/2000 = 0.0525 (<=0.10, passes).
        direct = _direct_itinerary(Decimal("2105.00"), itinerary_id="itin_or_2d")
        stop = _one_stop_itinerary(
            Decimal("2000.00"),
            itinerary_id="itin_or_2s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))
        assert analysis.tier == DirectTier.RECOMMENDED
        assert "relative-difference rule fired" in analysis.tier_reason
        assert "absolute-difference rule fired" not in analysis.tier_reason

    def test_good_value_absolute_arm_fires_relative_arm_fails(self) -> None:
        # diff = 150 (<=150, passes), rel = 150/500 = 0.30 (>0.20, fails).
        direct = _direct_itinerary(Decimal("650.00"), itinerary_id="itin_or_3d")
        stop = _one_stop_itinerary(
            Decimal("500.00"),
            itinerary_id="itin_or_3s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert "absolute-difference rule fired" in analysis.tier_reason

    def test_good_value_relative_arm_fires_absolute_arm_fails(self) -> None:
        # diff = 200 (>150, fails), rel = 200/1500 = 0.1333 (<=0.20, passes).
        direct = _direct_itinerary(Decimal("1700.00"), itinerary_id="itin_or_4d")
        stop = _one_stop_itinerary(
            Decimal("1500.00"),
            itinerary_id="itin_or_4s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert "relative-difference rule fired" in analysis.tier_reason


class TestZeroAndNoneGuards:
    """Every degenerate/edge shape named in the task brief: none of these
    may raise, especially not ``ZeroDivisionError``."""

    def test_cheapest_valid_stop_price_zero_skips_relative_rule_no_zero_division(self) -> None:
        direct = _direct_itinerary(Decimal("90.00"), itinerary_id="itin_zero_d")
        stop = _one_stop_itinerary(
            Decimal("0.00"),
            itinerary_id="itin_zero_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        # Must not raise ZeroDivisionError/decimal.DivisionByZero.
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.relative_difference is None
        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("90.00")
        # Absolute arm alone still decides a sensible tier: diff=90<=100.
        assert analysis.tier == DirectTier.RECOMMENDED

    def test_no_direct_service_is_not_available(self) -> None:
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_na_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(), one_stop_pool=(stop,))

        assert analysis.tier == DirectTier.NOT_AVAILABLE
        assert analysis.cheapest_direct is None
        assert analysis.price_difference is None
        assert analysis.relative_difference is None

    def test_no_valid_one_stop_alternative_does_not_crash(self) -> None:
        direct = _direct_itinerary(Decimal("900.00"), itinerary_id="itin_direct_only")
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=())

        assert analysis.cheapest_direct is not None
        assert analysis.cheapest_valid_stop is None
        assert analysis.price_difference is None
        assert analysis.relative_difference is None
        # A sensible tier still comes out -- see module docstring for why
        # RECOMMENDED, not NOT_RECOMMENDED/NOT_AVAILABLE, is the right default.
        assert analysis.tier == DirectTier.RECOMMENDED
        assert analysis.score_policy_divergence is False
        assert analysis.divergence_explanation is None

    def test_direct_cheaper_than_stop_is_recommended_not_naive_abs(self) -> None:
        # diff = 900 - 1200 = -300. A naive abs()-based check would compare
        # abs(-300) = 300 > 150 and wrongly land on NOT_RECOMMENDED; the
        # correct signed reading is diff <= 100 (true, since -300 <= 100),
        # so this must come out RECOMMENDED.
        direct = _direct_itinerary(Decimal("900.00"), itinerary_id="itin_cheaper_d")
        stop = _one_stop_itinerary(
            Decimal("1200.00"),
            itinerary_id="itin_cheaper_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("-300.00")
        assert analysis.tier == DirectTier.RECOMMENDED


class TestDecimalPrecisionAtTierBoundary:
    """Decimal, not float, comparisons: a difference of four-thousandths of
    a euro right at the 150 boundary must not be swallowed by float
    imprecision in either direction."""

    def test_150_00_passes_150_004_fails_the_same_boundary(self) -> None:
        thresholds = _SETTINGS.direct_tier
        # Relative arm deliberately fails (0.30 > 0.20) on both calls, so
        # only the absolute-difference arm can decide GOOD_VALUE vs
        # NOT_RECOMMENDED here.
        tier_at_boundary, _ = evaluate_direct_tier(Decimal("150.00"), Decimal("0.30"), thresholds)
        tier_just_over, _ = evaluate_direct_tier(Decimal("150.004"), Decimal("0.30"), thresholds)

        assert tier_at_boundary == DirectTier.GOOD_VALUE
        assert tier_just_over == DirectTier.NOT_RECOMMENDED


class TestScorePolicyDivergence:
    """Finding 0.1's own worked example, reproduced by hand:

    stop EUR1000 (15h total, one 250min layover -> band [240,300) -> +10),
    direct EUR1195 (8h total, zero layovers).

    diff = 195, rel = 0.195 -> fails RECOMMENDED (100/0.10) on both arms,
    passes GOOD_VALUE via the relative arm (0.195 <= 0.20) -> policy says
    "recommend direct".

    Scores (fixed -120 direct bonus, the packaged default):
      direct: 1195 + (8 * 3.0) - 120     = 1195 + 24 - 120 = 1099
      stop:   1000 + (15 * 3.0) + 10     = 1000 + 45 + 10  = 1055

    stop (1055) scores BETTER (lower) than direct (1099) -- the ranked list
    would place the one-stop first even though the tier recommends direct.
    That is the disagreement ``score_policy_divergence`` exists to surface.
    """

    def test_divergence_fires_above_755_worked_example(self) -> None:
        direct = _direct_itinerary(
            Decimal("1195.00"), itinerary_id="itin_div_direct", duration=timedelta(hours=8)
        )
        stop = _one_stop_itinerary(
            Decimal("1000.00"),
            itinerary_id="itin_div_stop",
            first_leg=timedelta(hours=5),
            layover_duration=timedelta(minutes=250),
            second_leg=timedelta(hours=5, minutes=50),
        )
        assert stop.total_duration == timedelta(hours=15)

        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.tier == DirectTier.GOOD_VALUE
        assert analysis.score_policy_divergence is True
        assert analysis.divergence_explanation is not None
        assert "1099" in analysis.divergence_explanation
        assert "1055" in analysis.divergence_explanation

    def test_divergence_does_not_fire_below_threshold(self) -> None:
        # Same shapes as the D10 worked case 1 (stop 620/direct 710):
        # direct adjusted = 710 + 24 - 120 = 614; stop adjusted (13.5h,
        # 210min layover -> 0 penalty) = 620 + 40.5 + 0 = 660.5. Direct
        # scores better AND the tier recommends it -- no disagreement.
        direct = _direct_itinerary(
            Decimal("710.00"), itinerary_id="itin_nodiv_direct", duration=timedelta(hours=8)
        )
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_nodiv_stop",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.tier == DirectTier.RECOMMENDED
        assert analysis.score_policy_divergence is False
        assert analysis.divergence_explanation is None


class TestTimeSaved:
    """T32: ``time_saved = cheapest_valid_stop.total_duration -
    cheapest_direct.total_duration``. Positive means the direct itinerary is
    faster; this can legitimately go negative, and must be exactly ``None``
    (not a fabricated zero) whenever either side of the comparison is
    missing."""

    def test_direct_seven_hours_faster_is_positive_exact_timedelta(self) -> None:
        # Direct: 8h total (see _direct_itinerary's default). One-stop: 6h
        # first leg + 3h30m layover + 5h30m second leg = 15h total.
        # time_saved = 15h - 8h = exactly 7h.
        direct = _direct_itinerary(
            Decimal("710.00"), itinerary_id="itin_time_direct_1", duration=timedelta(hours=8)
        )
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_time_stop_1",
            first_leg=timedelta(hours=6),
            layover_duration=timedelta(hours=3, minutes=30),
            second_leg=timedelta(hours=5, minutes=30),
        )
        assert stop.total_duration - direct.total_duration == timedelta(hours=7)

        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.time_saved == timedelta(hours=7)

    def test_direct_slower_than_stop_is_negative_not_clamped(self) -> None:
        # Direct: 16h total (a long-haul direct). One-stop: 2h + 3h30m
        # layover (210min -- inside D8's valid [180,360] layover window) +
        # 2h30m = 8h total (a fast connection). time_saved = 8h - 16h = -8h:
        # the direct flight is SLOWER, and this must surface as a real
        # negative timedelta, not be clamped to zero or raise.
        direct = _direct_itinerary(
            Decimal("710.00"), itinerary_id="itin_time_direct_2", duration=timedelta(hours=16)
        )
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_time_stop_2",
            first_leg=timedelta(hours=2),
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=timedelta(hours=2, minutes=30),
        )
        assert stop.total_duration - direct.total_duration == timedelta(hours=-8)

        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.time_saved == timedelta(hours=-8)
        assert analysis.time_saved is not None
        assert analysis.time_saved < timedelta(0)

    def test_no_direct_service_time_saved_is_none_not_zero(self) -> None:
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_time_na_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(), one_stop_pool=(stop,))

        assert analysis.tier == DirectTier.NOT_AVAILABLE
        assert analysis.time_saved is None

    def test_no_valid_one_stop_alternative_time_saved_is_none_not_zero(self) -> None:
        direct = _direct_itinerary(Decimal("900.00"), itinerary_id="itin_time_direct_only")
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=())

        assert analysis.cheapest_valid_stop is None
        assert analysis.time_saved is None


class TestConfigDrivenTierThresholds:
    """The D10 thresholds are genuinely read from config, not hardcoded --
    overriding ``[direct_tier]`` must change the outcome for the identical
    itinerary pair."""

    def test_stricter_override_flips_recommended_to_not_recommended(self) -> None:
        direct = _direct_itinerary(Decimal("710.00"), itinerary_id="itin_cfg_direct")
        stop = _one_stop_itinerary(
            Decimal("620.00"),
            itinerary_id="itin_cfg_stop",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )

        default_analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))
        assert default_analysis.tier == DirectTier.RECOMMENDED

        strict_settings = load_config(
            env={
                "FLIGHTAGENT__DIRECT_TIER__RECOMMENDED_MAX_DIFF_EUR": "1",
                "FLIGHTAGENT__DIRECT_TIER__RECOMMENDED_MAX_RELATIVE": "0.001",
                "FLIGHTAGENT__DIRECT_TIER__GOOD_VALUE_MAX_DIFF_EUR": "1",
                "FLIGHTAGENT__DIRECT_TIER__GOOD_VALUE_MAX_RELATIVE": "0.001",
            }
        )
        strict_analysis = _analyze(
            direct_pool=(direct,),
            one_stop_pool=(stop,),
            direct_tier_settings=strict_settings.direct_tier,
        )
        assert strict_analysis.tier == DirectTier.NOT_RECOMMENDED


class TestAbsoluteThresholdBoundaryAtStop600:
    """T35: the ``good_value_max_diff_eur`` (150) boundary in isolation, held
    at a fixed ``cheapest_valid_stop`` price (EUR600) chosen so the relative
    arm (150/600 == 0.25 at the pivot) fails throughout -- only the absolute
    arm can decide GOOD_VALUE vs NOT_RECOMMENDED here. DECISIONS.md D10's own
    wording is "diff <= 150" for GOOD_VALUE -- inclusive, so 150 itself must
    still pass."""

    def test_149_below_threshold_is_good_value(self) -> None:
        direct = _direct_itinerary(Decimal("749.00"), itinerary_id="itin_abs600_149d")
        stop = _one_stop_itinerary(
            Decimal("600.00"),
            itinerary_id="itin_abs600_149s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("149.00")
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert "absolute-difference rule fired" in analysis.tier_reason

    def test_exactly_150_is_inclusive_still_good_value(self) -> None:
        direct = _direct_itinerary(Decimal("750.00"), itinerary_id="itin_abs600_150d")
        stop = _one_stop_itinerary(
            Decimal("600.00"),
            itinerary_id="itin_abs600_150s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("150.00")
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert "absolute-difference rule fired" in analysis.tier_reason

    def test_151_above_threshold_and_relative_arm_also_fails_is_not_recommended(self) -> None:
        # rel = 151/600 = 0.25166... > good_value_max_relative (0.20), so
        # neither arm fires and the overall tier is NOT_RECOMMENDED, not a
        # near-miss that the relative arm happens to rescue.
        direct = _direct_itinerary(Decimal("751.00"), itinerary_id="itin_abs600_151d")
        stop = _one_stop_itinerary(
            Decimal("600.00"),
            itinerary_id="itin_abs600_151s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("151.00")
        assert analysis.relative_difference is not None
        assert analysis.relative_difference > Decimal("0.20")
        assert analysis.tier == DirectTier.NOT_RECOMMENDED
        assert "neither rule fired" in analysis.tier_reason


class TestRelativeThresholdBoundaryAtStop1000:
    """T35: the ``good_value_max_relative`` (0.20) boundary in isolation, at
    a round ``cheapest_valid_stop`` price (EUR1000) chosen so the absolute
    arm (200 and 201 EUR diff, both > good_value_max_diff_eur=150) fails
    throughout -- only the relative arm can decide here."""

    def test_exactly_0_20_is_inclusive_good_value(self) -> None:
        direct = _direct_itinerary(Decimal("1200.00"), itinerary_id="itin_rel1000_200d")
        stop = _one_stop_itinerary(
            Decimal("1000.00"),
            itinerary_id="itin_rel1000_200s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.relative_difference == Decimal("0.20")
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert "relative-difference rule fired" in analysis.tier_reason

    def test_0_201_just_above_threshold_is_not_recommended(self) -> None:
        direct = _direct_itinerary(Decimal("1201.00"), itinerary_id="itin_rel1000_201d")
        stop = _one_stop_itinerary(
            Decimal("1000.00"),
            itinerary_id="itin_rel1000_201s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.relative_difference == Decimal("0.201")
        assert analysis.tier == DirectTier.NOT_RECOMMENDED
        assert "neither rule fired" in analysis.tier_reason


class TestTierReasonNamesActualNumbers:
    """T35: ``tier_reason`` must name the actual numbers involved, not a
    generic "a threshold fired" message -- assert on the literal string, not
    merely its non-emptiness. Uses a pair where BOTH arms of RECOMMENDED
    pass simultaneously (diff=40<=100 AND rel=0.08<=0.10), a branch no
    existing T31/T32 test inspects the ``tier_reason`` text of."""

    def test_both_rules_fired_names_both_thresholds_and_both_actual_values(self) -> None:
        direct = _direct_itinerary(Decimal("540.00"), itinerary_id="itin_reason_both_d")
        stop = _one_stop_itinerary(
            Decimal("500.00"),
            itinerary_id="itin_reason_both_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.tier == DirectTier.RECOMMENDED
        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("40.00")
        assert analysis.relative_difference == Decimal("0.08")
        assert analysis.tier_reason == (
            "RECOMMENDED: both rules fired -- price difference EUR40.00 <= "
            "recommended_max_diff_eur EUR100 AND relative difference 0.08 <= "
            "recommended_max_relative 0.1"
        )

    def test_good_value_absolute_only_names_good_value_threshold_not_recommended_one(
        self,
    ) -> None:
        # diff=149<=150 (good_value passes) but diff=149>100 (recommended
        # absolute fails) and rel=149/600=0.2483..>0.20 (both relative arms
        # fail too) -- tier_reason must cite good_value's own threshold
        # number (150), never the inner recommended one (100).
        direct = _direct_itinerary(Decimal("749.00"), itinerary_id="itin_reason_gv_d")
        stop = _one_stop_itinerary(
            Decimal("600.00"),
            itinerary_id="itin_reason_gv_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.tier == DirectTier.GOOD_VALUE
        assert analysis.tier_reason == (
            "GOOD_VALUE: absolute-difference rule fired -- price difference EUR149.00 <= "
            "good_value_max_diff_eur EUR150"
        )


class TestDirectEqualPriceIsRecommended:
    """T35: ``price_difference`` of exactly zero (direct costs the same as
    the cheapest one-stop) is a free upgrade to no connection -- must land
    RECOMMENDED, not merely "not NOT_RECOMMENDED"."""

    def test_direct_equal_price_chooses_direct(self) -> None:
        direct = _direct_itinerary(Decimal("500.00"), itinerary_id="itin_equal_d")
        stop = _one_stop_itinerary(
            Decimal("500.00"),
            itinerary_id="itin_equal_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("0.00")
        assert analysis.relative_difference == Decimal("0.00")
        assert analysis.tier == DirectTier.RECOMMENDED
        assert "both rules fired" in analysis.tier_reason


class TestDecimalNotFloatAtBoundary:
    """T35: an adversarial Decimal pair chosen so IEEE-754 double subtraction
    of the same two numbers overshoots the 150 boundary by a
    float-rounding artifact (confirmed by hand: ``256.47 - 106.47 ==
    150.00000000000003`` in binary float64), while exact Decimal
    subtraction lands precisely on ``150.00``. If ``analyze_destination``
    ever regressed to comparing prices as ``float`` instead of ``Decimal``,
    this pair would misclassify as NOT_RECOMMENDED instead of the correct
    GOOD_VALUE."""

    def test_prices_compared_as_decimal_not_float(self) -> None:
        direct_price = Decimal("256.47")
        stop_price = Decimal("106.47")
        # Sanity check the adversarial premise itself: float subtraction of
        # these exact two numbers does NOT land on 150.0.
        assert float(direct_price) - float(stop_price) != 150.0

        direct = _direct_itinerary(direct_price, itinerary_id="itin_decimal_adversarial_d")
        stop = _one_stop_itinerary(
            stop_price,
            itinerary_id="itin_decimal_adversarial_s",
            first_leg=_WORKED_STOP_FIRST_LEG,
            layover_duration=_WORKED_STOP_LAYOVER,
            second_leg=_WORKED_STOP_SECOND_LEG,
        )
        analysis = _analyze(direct_pool=(direct,), one_stop_pool=(stop,))

        assert analysis.price_difference is not None
        assert analysis.price_difference.amount == Decimal("150.00")
        assert analysis.tier == DirectTier.GOOD_VALUE
        assert "absolute-difference rule fired" in analysis.tier_reason
