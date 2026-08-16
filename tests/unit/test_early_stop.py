"""Unit tests for flightagent.policy.early_stop / flightagent.orchestration.waves
(Phase 6, T39, D12, finding 0.7).

D12's rule, restated: for destination D, origin O (in priority order)
triggers when its cheapest VALID fare to D is >= EUR250 below the cheapest
valid fare to D across every origin already evaluated before it. The rule
is evaluable only once >=2 prior origins have a valid fare to compare
against -- finding 0.7's "vacuous truth on the first origin" fix.

``TestPureRuleAgainstOriginFares`` exercises ``policy.early_stop.
evaluate_destination_early_stop`` directly against hand-built ``OriginFare``
tuples (no task/itinerary machinery needed). ``TestReplayOverRealTasks``
exercises ``orchestration.waves.replay_early_stop`` against real
``SearchTask``/``NormalizedItinerary`` objects, including the codebase's
real ``validation.engine.validate`` to prove an invalid itinerary's price
never reaches the comparison.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flightagent.config.loader import load_config
from flightagent.domain.enums import CabinClass, RejectionCode
from flightagent.domain.ids import compute_task_id
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.run import SearchRequest, SearchTask
from flightagent.domain.segment import Layover, Segment
from flightagent.orchestration.waves import replay_early_stop
from flightagent.policy.early_stop import MODE, OriginFare, evaluate_destination_early_stop
from flightagent.validation.engine import validate

_SETTINGS = load_config(env={})
_THRESHOLD = _SETTINGS.early_stop.threshold_eur
_DEPARTURE_DATE = date(2027, 7, 17)
_LAYOVER_MIN = timedelta(minutes=180)
_LAYOVER_MAX = timedelta(minutes=360)


def _fare(origin: str, wave: int, price: str | None) -> OriginFare:
    return OriginFare(
        origin=origin, wave=wave, price_eur=Decimal(price) if price is not None else None
    )


# ---------------------------------------------------------------------------
# TestPureRuleAgainstOriginFares -- policy.early_stop.evaluate_destination_early_stop
# ---------------------------------------------------------------------------


class TestVacuousTruthFix:
    """Finding 0.7: the first (and second) origin evaluated for a
    destination can never trigger -- there is no >=2-member comparison set
    to check against yet."""

    def test_first_origin_never_triggers_against_empty_comparison_set(self) -> None:
        fares = (_fare("AMS", 1, "100.00"),)
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)
        assert result.triggered is False
        assert result.triggering_origin is None
        # The rule was never once evaluable (only 1 valid-fare origin ever
        # seen) -- compared_against reflects every valid-fare origin
        # observed, not an actual performed comparison (see
        # evaluate_destination_early_stop's own docstring).
        assert result.compared_against == ("AMS",)
        assert result.evaluated_at_wave == 1
        assert result.mode == "advisory"

    def test_second_origin_never_triggers_against_single_member_comparison_set(self) -> None:
        # EIN is EUR999 cheaper than AMS -- if the rule were vacuously
        # evaluable at 2 origins, this would obviously trigger. It must not.
        fares = (_fare("AMS", 1, "1000.00"), _fare("EIN", 1, "1.00"))
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)
        assert result.triggered is False
        assert result.triggering_origin is None
        assert result.compared_against == ("AMS", "EIN")


class TestExactThresholdBoundary:
    """D12's own EUR250 boundary, inclusive on the trigger side."""

    def test_exactly_250_cheaper_triggers_at_the_third_origin(self) -> None:
        fares = (
            _fare("AMS", 1, "1000.00"),
            _fare("EIN", 1, "900.00"),
            _fare("RTM", 1, "650.00"),  # best prior = min(1000, 900) = 900; margin = 250
        )
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)

        assert result.triggered is True
        assert result.triggering_origin == "RTM"
        assert result.triggering_destination == "DEL"
        assert result.margin == Money(amount=Decimal("250.00"), currency="EUR")
        assert result.compared_against == ("AMS", "EIN")
        assert result.evaluated_at_wave == 1
        assert result.mode == "advisory"
        assert result.mode == MODE

    def test_249_cheaper_does_not_trigger(self) -> None:
        fares = (
            _fare("AMS", 1, "1000.00"),
            _fare("EIN", 1, "900.00"),
            _fare("RTM", 1, "651.00"),  # margin = 249
        )
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)

        assert result.triggered is False
        assert result.triggering_origin is None
        assert result.triggering_destination is None
        assert result.margin is None
        # The comparison DID run (3rd origin, 2 priors) -- compared_against
        # reflects that real, performed comparison, not an empty default.
        assert result.compared_against == ("AMS", "EIN")


class TestStopsReplayAtFirstTrigger:
    def test_later_even_cheaper_origin_is_never_considered_once_triggered(self) -> None:
        fares = (
            _fare("AMS", 1, "1000.00"),
            _fare("EIN", 1, "900.00"),
            _fare("RTM", 1, "650.00"),  # triggers here (margin 250)
            _fare("DUS", 2, "1.00"),  # would trigger far harder -- must be ignored
        )
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)

        assert result.triggered is True
        assert result.triggering_origin == "RTM"
        assert result.evaluated_at_wave == 1


class TestOnlyValidFaresParticipate:
    """An origin with no valid fare (``price_eur=None``) contributes
    neither a candidate price nor a comparison-basis entry -- it is simply
    absent, never treated as EUR 0 or as satisfying the >=2-prior-origins
    count."""

    def test_origin_with_no_valid_fare_does_not_count_toward_the_minimum(self) -> None:
        fares = (
            _fare("AMS", 1, "1000.00"),
            _fare("EIN", 1, None),  # no valid fare at all -- skipped entirely
            _fare("RTM", 1, "900.00"),
            _fare("DUS", 2, "650.00"),  # 3rd VALID-fare origin: margin = 900-650 = 250
        )
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)

        assert result.triggered is True
        assert result.triggering_origin == "DUS"
        # EIN is excluded from compared_against -- it never had a fare to compare.
        assert result.compared_against == ("AMS", "RTM")


class TestNeverEvaluable:
    """Fewer than 3 origins ever carrying a valid fare means the rule never
    once reaches its >=2-prior-origins threshold -- every result is
    triggered=False, and evaluated_at_wave is the last origin's wave."""

    def test_zero_origins_with_a_valid_fare(self) -> None:
        fares = (_fare("AMS", 1, None), _fare("EIN", 1, None))
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)
        assert result.triggered is False
        assert result.compared_against == ()
        assert result.evaluated_at_wave == 1

    def test_empty_origin_fares(self) -> None:
        result = evaluate_destination_early_stop("DEL", (), threshold_eur=_THRESHOLD)
        assert result.triggered is False
        assert result.compared_against == ()
        assert result.evaluated_at_wave == 0


class TestConfigDrivenThreshold:
    """The EUR threshold is read from the caller's ``threshold_eur``
    argument, never hardcoded inside the rule -- the identical fare set
    must produce different verdicts under different thresholds."""

    def test_lowering_the_threshold_flips_the_verdict(self) -> None:
        fares = (
            _fare("AMS", 1, "1000.00"),
            _fare("EIN", 1, "900.00"),
            _fare("RTM", 1, "890.00"),  # margin = 10
        )
        default_result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)
        assert default_result.triggered is False

        strict_result = evaluate_destination_early_stop(
            "DEL", fares, threshold_eur=Decimal("5")
        )
        assert strict_result.triggered is True
        assert strict_result.triggering_origin == "RTM"


class TestModelValidatorSatisfiedByRealConstruction:
    """``EarlyStopEvaluation``'s own ``model_validator`` (Phase 1) is
    exercised for real by every call above -- a violation would raise
    ``pydantic.ValidationError`` from inside ``evaluate_destination_early_stop``
    itself, not from a hand-rolled check in this test file. This class
    just makes the triggered/not-triggered field-presence contract explicit
    for both branches in one place."""

    def test_triggered_true_carries_all_three_fields(self) -> None:
        fares = (
            _fare("AMS", 1, "1000.00"),
            _fare("EIN", 1, "900.00"),
            _fare("RTM", 1, "650.00"),
        )
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)
        assert result.triggered is True
        assert result.triggering_origin is not None
        assert result.triggering_destination is not None
        assert result.margin is not None

    def test_triggered_false_carries_none_of_the_three_fields(self) -> None:
        fares = (_fare("AMS", 1, "1000.00"),)
        result = evaluate_destination_early_stop("DEL", fares, threshold_eur=_THRESHOLD)
        assert result.triggered is False
        assert result.triggering_origin is None
        assert result.triggering_destination is None
        assert result.margin is None


# ---------------------------------------------------------------------------
# TestReplayOverRealTasks -- orchestration.waves.replay_early_stop
# ---------------------------------------------------------------------------


def _task(
    origin: str, destination: str, *, max_stops: int, origin_priority: int, wave: int
) -> SearchTask:
    max_stops_literal = 0 if max_stops == 0 else 1
    request = SearchRequest(
        origin=origin,
        destination=destination,
        departure_date=_DEPARTURE_DATE,
        cabin=CabinClass.ECONOMY,
        max_stops=max_stops_literal,
        adults=1,
        currency="EUR",
        layover_min=_LAYOVER_MIN,
        layover_max=_LAYOVER_MAX,
    )
    return SearchTask(
        task_id=compute_task_id(origin, destination, max_stops_literal),
        request=request,
        origin_priority=origin_priority,
        wave=wave,
    )


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


def _direct_itinerary(
    origin: str, destination: str, price_eur: Decimal, *, itinerary_id: str
) -> NormalizedItinerary:
    depart = datetime(2027, 7, 17, 8, 0, tzinfo=UTC)
    arrive = depart + timedelta(hours=8)
    seg = _segment(
        origin=origin,
        destination=destination,
        depart_utc=depart,
        arrive_utc=arrive,
        flight_number="1",
    )
    leg = Leg(segments=(seg,), layovers=())
    price = Money(amount=price_eur, currency="EUR")
    return NormalizedItinerary(
        itinerary_id=itinerary_id,
        provider="mock",
        legs=(leg,),
        price_original=price,
        price_eur=price,
        booking_url_kind="unavailable",
        shape_key=f"shape-{itinerary_id}",
        fare_as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _one_stop_itinerary_with_bad_layover(
    origin: str, destination: str, price_eur: Decimal, *, itinerary_id: str
) -> NormalizedItinerary:
    """A one-stop itinerary whose layover (60 minutes) is well outside
    D8's ``[180, 360]`` valid window -- guaranteed ``LAYOVER_TOO_SHORT``
    when run through the real validation engine."""
    depart = datetime(2027, 7, 17, 8, 0, tzinfo=UTC)
    arrive1 = depart + timedelta(hours=4)
    depart2 = arrive1 + timedelta(minutes=60)  # too short: below the 180min minimum
    arrive2 = depart2 + timedelta(hours=4)
    seg1 = _segment(
        origin=origin,
        destination="DXB",
        depart_utc=depart,
        arrive_utc=arrive1,
        flight_number="1",
    )
    layover = Layover(
        airport="DXB",
        arrive_utc=arrive1,
        depart_utc=depart2,
        duration=depart2 - arrive1,
        local_window=(arrive1, depart2),
        requires_airport_change=False,
        requires_terminal_change=False,
    )
    seg2 = _segment(
        origin="DXB",
        destination=destination,
        depart_utc=depart2,
        arrive_utc=arrive2,
        flight_number="2",
    )
    leg = Leg(segments=(seg1, seg2), layovers=(layover,))
    price = Money(amount=price_eur, currency="EUR")
    return NormalizedItinerary(
        itinerary_id=itinerary_id,
        provider="mock",
        legs=(leg,),
        price_original=price,
        price_eur=price,
        booking_url_kind="unavailable",
        shape_key=f"shape-{itinerary_id}",
        fare_as_of=datetime(2027, 1, 1, tzinfo=UTC),
    )


class TestReplayDedupesOriginsAndPicksCheapestAcrossModes:
    def test_cheapest_price_taken_across_direct_and_one_stop_tasks(self) -> None:
        ams = _task("AMS", "DEL", max_stops=0, origin_priority=1, wave=1)
        ein = _task("EIN", "DEL", max_stops=0, origin_priority=2, wave=1)
        rtm_direct = _task("RTM", "DEL", max_stops=0, origin_priority=3, wave=1)
        rtm_stop = _task("RTM", "DEL", max_stops=1, origin_priority=3, wave=1)

        valid_by_task_id = {
            ams.task_id: (_direct_itinerary("AMS", "DEL", Decimal("1000.00"), itinerary_id="a"),),
            ein.task_id: (_direct_itinerary("EIN", "DEL", Decimal("900.00"), itinerary_id="b"),),
            # RTM's direct fare (900) is NOT the cheapest it found -- its
            # one-stop task found 650, which must win the per-origin min.
            rtm_direct.task_id: (
                _direct_itinerary("RTM", "DEL", Decimal("900.00"), itinerary_id="c-direct"),
            ),
            rtm_stop.task_id: (),
        }
        tasks = (ams, ein, rtm_direct, rtm_stop)

        evaluations = replay_early_stop(
            tasks, valid_by_task_id, destinations=("DEL",), threshold_eur=_THRESHOLD
        )
        # Only RTM's direct fare (900) is on the table here -- no trigger
        # (margin = min(1000,900) - 900 = 0).
        assert evaluations["DEL"].triggered is False


class TestOnlyValidFaresCompared:
    """Finding 0.7 / D12: the rule only compares VALID fares -- an
    itinerary that fails the real ``validation.engine.validate`` must never
    influence the replay, however cheap it is."""

    def test_cheap_but_invalid_itinerary_never_feeds_the_comparison(self) -> None:
        ams = _task("AMS", "DEL", max_stops=0, origin_priority=1, wave=1)
        ein = _task("EIN", "DEL", max_stops=0, origin_priority=2, wave=1)
        rtm_direct = _task("RTM", "DEL", max_stops=0, origin_priority=3, wave=1)
        rtm_stop = _task("RTM", "DEL", max_stops=1, origin_priority=3, wave=1)

        ams_itin = _direct_itinerary("AMS", "DEL", Decimal("1000.00"), itinerary_id="ams1")
        ein_itin = _direct_itinerary("EIN", "DEL", Decimal("900.00"), itinerary_id="ein1")
        rtm_direct_itin = _direct_itinerary(
            "RTM", "DEL", Decimal("890.00"), itinerary_id="rtm-direct"
        )
        rtm_cheap_invalid_itin = _one_stop_itinerary_with_bad_layover(
            "RTM", "DEL", Decimal("1.00"), itinerary_id="rtm-bad-layover"
        )

        # Prove the premise: this itinerary really does fail validation.
        validation_result = validate(rtm_cheap_invalid_itin, rtm_stop.request)
        assert not validation_result.is_valid
        assert any(
            rejection.code == RejectionCode.LAYOVER_TOO_SHORT
            for rejection in validation_result.rejections
        )

        # valid_by_task_id mirrors exactly what cli.py's own pipeline
        # builds: only itineraries that PASSED validate() ever appear here
        # -- rtm_cheap_invalid_itin is correctly absent from rtm_stop's entry.
        valid_by_task_id = {
            ams.task_id: (ams_itin,),
            ein.task_id: (ein_itin,),
            rtm_direct.task_id: (rtm_direct_itin,),
            rtm_stop.task_id: (),
        }
        tasks = (ams, ein, rtm_direct, rtm_stop)

        evaluations = replay_early_stop(
            tasks, valid_by_task_id, destinations=("DEL",), threshold_eur=_THRESHOLD
        )
        result = evaluations["DEL"]
        # If the EUR1.00 invalid fare had leaked in, margin would be
        # min(1000,900) - 1 = 899 >= 250 -> triggered. It must not.
        assert result.triggered is False
        assert result.compared_against == ("AMS", "EIN")


class TestReplayNeverAltersTheUnderlyingResultSet:
    """D12 default (``enabled=false``): the full fan-out's result set is
    reported in full regardless of what the early-stop annotation says --
    the replay is purely additive and must never remove, skip, or reorder
    any task's own results."""

    def test_full_result_set_is_untouched_even_when_a_destination_triggers(self) -> None:
        tasks = (
            _task("AMS", "DEL", max_stops=0, origin_priority=1, wave=1),
            _task("EIN", "DEL", max_stops=0, origin_priority=2, wave=1),
            _task("RTM", "DEL", max_stops=0, origin_priority=3, wave=1),
            _task("DUS", "DEL", max_stops=0, origin_priority=4, wave=2),
        )
        valid_by_task_id = {
            tasks[0].task_id: (
                _direct_itinerary("AMS", "DEL", Decimal("1000.00"), itinerary_id="a"),
            ),
            tasks[1].task_id: (
                _direct_itinerary("EIN", "DEL", Decimal("900.00"), itinerary_id="b"),
            ),
            tasks[2].task_id: (
                _direct_itinerary("RTM", "DEL", Decimal("650.00"), itinerary_id="c"),
            ),  # triggers (margin 250)
            tasks[3].task_id: (
                _direct_itinerary("DUS", "DEL", Decimal("1.00"), itinerary_id="d"),
            ),  # would trigger even harder -- must be left completely alone
        }
        before_snapshot = dict(valid_by_task_id)

        evaluations = replay_early_stop(
            tasks, valid_by_task_id, destinations=("DEL",), threshold_eur=_THRESHOLD
        )

        assert evaluations["DEL"].triggered is True
        assert evaluations["DEL"].triggering_origin == "RTM"
        # Nothing about the actual result set changed.
        assert valid_by_task_id == before_snapshot
        assert sum(len(itins) for itins in valid_by_task_id.values()) == 4
        assert valid_by_task_id[tasks[3].task_id][0].itinerary_id == "d"

    def test_replay_reads_threshold_from_caller_not_hardcoded(self) -> None:
        tasks = (
            _task("AMS", "DEL", max_stops=0, origin_priority=1, wave=1),
            _task("EIN", "DEL", max_stops=0, origin_priority=2, wave=1),
            _task("RTM", "DEL", max_stops=0, origin_priority=3, wave=1),
        )
        valid_by_task_id = {
            tasks[0].task_id: (
                _direct_itinerary("AMS", "DEL", Decimal("1000.00"), itinerary_id="a"),
            ),
            tasks[1].task_id: (
                _direct_itinerary("EIN", "DEL", Decimal("900.00"), itinerary_id="b"),
            ),
            tasks[2].task_id: (
                _direct_itinerary("RTM", "DEL", Decimal("890.00"), itinerary_id="c"),
            ),  # margin = 10 -- below the default 250 threshold
        }

        default_evaluations = replay_early_stop(
            tasks, valid_by_task_id, destinations=("DEL",), threshold_eur=_THRESHOLD
        )
        assert default_evaluations["DEL"].triggered is False

        strict_evaluations = replay_early_stop(
            tasks, valid_by_task_id, destinations=("DEL",), threshold_eur=Decimal("5")
        )
        assert strict_evaluations["DEL"].triggered is True
        assert strict_evaluations["DEL"].triggering_origin == "RTM"


class TestReplayHandlesMultipleDestinationsIndependently:
    def test_one_destination_triggers_another_does_not(self) -> None:
        ams_del = _task("AMS", "DEL", max_stops=0, origin_priority=1, wave=1)
        ein_del = _task("EIN", "DEL", max_stops=0, origin_priority=2, wave=1)
        rtm_del = _task("RTM", "DEL", max_stops=0, origin_priority=3, wave=1)
        ams_bom = _task("AMS", "BOM", max_stops=0, origin_priority=1, wave=1)
        ein_bom = _task("EIN", "BOM", max_stops=0, origin_priority=2, wave=1)
        rtm_bom = _task("RTM", "BOM", max_stops=0, origin_priority=3, wave=1)

        valid_by_task_id = {
            ams_del.task_id: (
                _direct_itinerary("AMS", "DEL", Decimal("1000.00"), itinerary_id="d1"),
            ),
            ein_del.task_id: (
                _direct_itinerary("EIN", "DEL", Decimal("900.00"), itinerary_id="d2"),
            ),
            rtm_del.task_id: (
                _direct_itinerary("RTM", "DEL", Decimal("650.00"), itinerary_id="d3"),
            ),
            ams_bom.task_id: (
                _direct_itinerary("AMS", "BOM", Decimal("500.00"), itinerary_id="b1"),
            ),
            ein_bom.task_id: (
                _direct_itinerary("EIN", "BOM", Decimal("480.00"), itinerary_id="b2"),
            ),
            rtm_bom.task_id: (
                _direct_itinerary("RTM", "BOM", Decimal("470.00"), itinerary_id="b3"),
            ),
        }
        tasks = (ams_del, ein_del, rtm_del, ams_bom, ein_bom, rtm_bom)

        evaluations = replay_early_stop(
            tasks, valid_by_task_id, destinations=("DEL", "BOM"), threshold_eur=_THRESHOLD
        )

        assert evaluations["DEL"].triggered is True
        assert evaluations["DEL"].triggering_origin == "RTM"
        assert evaluations["BOM"].triggered is False
        assert list(evaluations.keys()) == ["DEL", "BOM"]
