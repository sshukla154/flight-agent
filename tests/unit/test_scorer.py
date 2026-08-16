"""Unit tests for flightagent.scoring (T13, scorer v1).

Master plan finding 0.8 / DECISIONS.md D9: the layover penalty bands are
LOWER-INCLUSIVE HALF-OPEN, and the spec's own worked example (a 4-hour =
240-minute layover) lands exactly on the first boundary. The two tests
named explicitly in this task's brief —
``test_exactly_240_minutes_scores_10_not_0`` and
``test_exactly_300_minutes_scores_20_not_10`` — exist to catch an
off-by-one band read before it reaches a golden file.

Finding 0.3: every score component must be ``Decimal``, never ``float`` —
``TestAllComponentsAreDecimalNeverFloat`` checks the type directly, not
just the value, because a value that happens to compare equal to a float
would still hide the nondeterminism finding 0.3 is about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from flightagent.config.loader import load_config
from flightagent.config.models import LayoverSettings, PenaltyBand
from flightagent.domain.enums import CabinClass
from flightagent.domain.itinerary import Leg, NormalizedItinerary
from flightagent.domain.money import Money
from flightagent.domain.segment import Layover, Segment
from flightagent.scoring.components import layover_penalty_for_minutes
from flightagent.scoring.score import score_itinerary

_SETTINGS = load_config(env={})
_BANDS = _SETTINGS.layover.penalty_bands


def _segment(
    *,
    origin: str,
    destination: str,
    depart_utc: datetime,
    arrive_utc: datetime,
    flight_number: str,
) -> Segment:
    """UTC-zoned on both ends so depart_local/arrive_local trivially match
    depart_utc/arrive_utc — DST/fold correctness is covered elsewhere
    (test_domain_smoke.py); this file only cares about score arithmetic."""
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
    *,
    legs: tuple[Leg, ...],
    price_eur: Decimal,
    itinerary_id: str = "itin_test_0001",
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


def _worked_example_itinerary() -> NormalizedItinerary:
    """Master plan's / DECISIONS.md's own worked case: price 620, total
    duration 13h30m, one layover of 3h30m (210 min, band [180,240) -> 0).

    AMS(08:00Z) -> DXB(14:00Z) [6h] -- 210min layover at DXB -- DXB(17:30Z)
    -> DEL(21:30Z) [4h]. Endpoints total = 21:30 - 08:00 = 13h30m, matching
    segments(6h+4h=10h) + layover(3h30m) exactly, per Leg's own invariant.
    """
    seg1 = _segment(
        origin="AMS",
        destination="DXB",
        depart_utc=datetime(2027, 7, 17, 8, 0, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 14, 0, tzinfo=UTC),
        flight_number="431",
    )
    layover = _layover(
        airport="DXB",
        arrive_utc=datetime(2027, 7, 17, 14, 0, tzinfo=UTC),
        depart_utc=datetime(2027, 7, 17, 17, 30, tzinfo=UTC),
    )
    seg2 = _segment(
        origin="DXB",
        destination="DEL",
        depart_utc=datetime(2027, 7, 17, 17, 30, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 21, 30, tzinfo=UTC),
        flight_number="512",
    )
    leg = Leg(segments=(seg1, seg2), layovers=(layover,))
    return _itinerary(legs=(leg,), price_eur=Decimal("620.00"))


def _direct_itinerary() -> NormalizedItinerary:
    """A single-segment, zero-layover itinerary — stop_count == 0."""
    seg = _segment(
        origin="AMS",
        destination="DEL",
        depart_utc=datetime(2027, 7, 17, 8, 0, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 16, 0, tzinfo=UTC),
        flight_number="872",
    )
    leg = Leg(segments=(seg,), layovers=())
    assert leg.connection_count == 0
    return _itinerary(legs=(leg,), price_eur=Decimal("900.00"), itinerary_id="itin_direct_0001")


def _two_layover_itinerary() -> NormalizedItinerary:
    """Three segments / two layovers on ONE leg, to prove the sum is over
    however many layovers exist rather than hardcoded to "exactly one".

    AMS(00:00Z)->FRA(02:00Z)[2h] -- 200min layover (band 0) --
    FRA(05:20Z)->IST(07:20Z)[2h] -- 250min layover (band 10) --
    IST(11:30Z)->DEL(13:30Z)[2h]. Expected total layover_penalty = 0+10=10.
    """
    seg_a = _segment(
        origin="AMS",
        destination="FRA",
        depart_utc=datetime(2027, 7, 17, 0, 0, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 2, 0, tzinfo=UTC),
        flight_number="101",
    )
    layover_1 = _layover(
        airport="FRA",
        arrive_utc=datetime(2027, 7, 17, 2, 0, tzinfo=UTC),
        depart_utc=datetime(2027, 7, 17, 5, 20, tzinfo=UTC),
    )
    seg_b = _segment(
        origin="FRA",
        destination="IST",
        depart_utc=datetime(2027, 7, 17, 5, 20, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 7, 20, tzinfo=UTC),
        flight_number="202",
    )
    layover_2 = _layover(
        airport="IST",
        arrive_utc=datetime(2027, 7, 17, 7, 20, tzinfo=UTC),
        depart_utc=datetime(2027, 7, 17, 11, 30, tzinfo=UTC),
    )
    seg_c = _segment(
        origin="IST",
        destination="DEL",
        depart_utc=datetime(2027, 7, 17, 11, 30, tzinfo=UTC),
        arrive_utc=datetime(2027, 7, 17, 13, 30, tzinfo=UTC),
        flight_number="303",
    )
    leg = Leg(segments=(seg_a, seg_b, seg_c), layovers=(layover_1, layover_2))
    return _itinerary(
        legs=(leg,), price_eur=Decimal("700.00"), itinerary_id="itin_two_layover_0001"
    )


class TestLayoverPenaltyBandBoundaries:
    """D9's lower-inclusive half-open bands, parameterized across every
    boundary named in the task brief."""

    @pytest.mark.parametrize("minutes", [180, 200, 239])
    def test_below_240_scores_0(self, minutes: int) -> None:
        assert layover_penalty_for_minutes(minutes, _BANDS) == Decimal("0")

    def test_exactly_240_minutes_scores_10_not_0(self) -> None:
        """The named risk: the spec's own 4-hour sample lands here. Under
        D9's lower-inclusive half-open reading this is +10, NOT 0."""
        assert layover_penalty_for_minutes(240, _BANDS) == Decimal("10")

    @pytest.mark.parametrize("minutes", [241, 299])
    def test_241_to_299_scores_10(self, minutes: int) -> None:
        assert layover_penalty_for_minutes(minutes, _BANDS) == Decimal("10")

    def test_exactly_300_minutes_scores_20_not_10(self) -> None:
        """The second named boundary risk: exactly 300 minutes is +20, not
        the +10 of the band immediately below it."""
        assert layover_penalty_for_minutes(300, _BANDS) == Decimal("20")

    @pytest.mark.parametrize("minutes", [301, 360])
    def test_301_to_360_scores_20(self, minutes: int) -> None:
        assert layover_penalty_for_minutes(minutes, _BANDS) == Decimal("20")


class TestDirectItineraryLayoverPenalty:
    def test_direct_itinerary_has_zero_layover_penalty(self) -> None:
        components = score_itinerary(
            _direct_itinerary(),
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        assert components.layover_penalty == Decimal("0")


class TestMultipleLayoversSum:
    def test_two_layovers_sum_their_individual_band_penalties(self) -> None:
        components = score_itinerary(
            _two_layover_itinerary(),
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        # 200min -> band [180,240) -> 0 ; 250min -> band [240,300) -> 10.
        assert components.layover_penalty == Decimal("10")


class TestWorkedFullFormulaExample:
    """DECISIONS.md's / master plan's own worked case, computed by hand:

    score = price + (duration_hours * time_value_eur_per_hour) + layover_penalty
          = 620 + (13.5 * 3.0) + 0
          = 620 + 40.5 + 0
          = 660.5
    """

    def test_worked_example_matches_hand_computed_score(self) -> None:
        itinerary = _worked_example_itinerary()
        assert itinerary.total_duration == timedelta(hours=13, minutes=30)

        components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )

        assert components.fare_eur == Decimal("620.00")
        assert components.elapsed_time_component == Decimal("40.5")
        assert components.layover_penalty == Decimal("0")
        assert components.direct_bonus == Decimal("0")
        assert components.score == Decimal("660.5")
        assert components.adjusted_score == Decimal("660.5")


class TestDirectBonusAlwaysZeroForStopItineraries:
    """T30/Phase-5: direct_bonus is ALWAYS exactly Decimal("0") for any
    itinerary with stop_count >= 1, regardless of direct_bonus_mode — this
    must NOT regress now that a direct (stop_count == 0) itinerary genuinely
    gets a nonzero bonus. ``_worked_example_itinerary()`` (stop_count == 1)
    and ``_two_layover_itinerary()`` (stop_count == 2) are the itineraries
    this still applies to; ``_direct_itinerary()`` moved to its own class
    below since it now legitimately gets a nonzero bonus."""

    @pytest.mark.parametrize("mode", ["fixed", "proportional"])
    def test_stop_itinerary_bonus_is_always_exactly_decimal_zero(self, mode: str) -> None:
        settings = load_config(env={"FLIGHTAGENT__SCORING__DIRECT_BONUS_MODE": mode})
        for itinerary in (_worked_example_itinerary(), _two_layover_itinerary()):
            components = score_itinerary(
                itinerary,
                scoring_settings=settings.scoring,
                layover_settings=settings.layover,
                cheapest_valid_stop_price_eur=Decimal("1000.00"),
            )
            assert components.direct_bonus == Decimal("0")
            # Not just numerically equal to 0 — must not have picked up
            # config's -120.0 direct_bonus_eur by accident.
            assert components.direct_bonus != settings.scoring.direct_bonus_eur


class TestDirectBonusFixedMode:
    """T30: a direct (stop_count == 0) itinerary under "fixed" mode gets
    exactly ``settings.scoring.direct_bonus_eur`` — the flat -120.0
    default — as a ``Decimal``, never a float, and never re-derived from
    the fare."""

    def test_direct_itinerary_bonus_equals_configured_fixed_eur_amount(self) -> None:
        assert _SETTINGS.scoring.direct_bonus_mode == "fixed"
        components = score_itinerary(
            _direct_itinerary(),
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        assert components.direct_bonus == _SETTINGS.scoring.direct_bonus_eur
        assert components.direct_bonus == Decimal("-120.0")
        assert isinstance(components.direct_bonus, Decimal)
        assert not isinstance(components.direct_bonus, float)


class TestDirectBonusProportionalMode:
    """T30: "proportional" mode scales the bonus to
    ``-0.20 * cheapest_valid_stop_price_eur`` (finding 0.1, master plan
    §0.1's own proposed resolution) instead of the flat -120.0, and the two
    modes must produce genuinely different, correctly-scaled numbers on the
    same itinerary — not just a config flag with no arithmetic effect.

    Worked example, computed by hand: cheapest_valid_stop_price_eur =
    Decimal("1000.00") -> -0.20 * 1000.00 = Decimal("-200.0000"), a bonus
    50% deeper in magnitude than the -120.0 fixed default.
    """

    def test_proportional_bonus_matches_hand_computed_value(self) -> None:
        settings = load_config(env={"FLIGHTAGENT__SCORING__DIRECT_BONUS_MODE": "proportional"})
        components = score_itinerary(
            _direct_itinerary(),
            scoring_settings=settings.scoring,
            layover_settings=settings.layover,
            cheapest_valid_stop_price_eur=Decimal("1000.00"),
        )
        assert components.direct_bonus == Decimal("-200.0000")
        assert isinstance(components.direct_bonus, Decimal)
        assert not isinstance(components.direct_bonus, float)

    def test_proportional_differs_from_fixed_on_the_same_itinerary(self) -> None:
        fixed_settings = load_config(env={"FLIGHTAGENT__SCORING__DIRECT_BONUS_MODE": "fixed"})
        proportional_settings = load_config(
            env={"FLIGHTAGENT__SCORING__DIRECT_BONUS_MODE": "proportional"}
        )
        itinerary = _direct_itinerary()

        fixed_components = score_itinerary(
            itinerary,
            scoring_settings=fixed_settings.scoring,
            layover_settings=fixed_settings.layover,
            cheapest_valid_stop_price_eur=Decimal("1000.00"),
        )
        proportional_components = score_itinerary(
            itinerary,
            scoring_settings=proportional_settings.scoring,
            layover_settings=proportional_settings.layover,
            cheapest_valid_stop_price_eur=Decimal("1000.00"),
        )

        assert fixed_components.direct_bonus == Decimal("-120.0")
        assert proportional_components.direct_bonus == Decimal("-200.0000")
        assert proportional_components.direct_bonus != fixed_components.direct_bonus

    def test_proportional_mode_without_cheapest_stop_price_raises(self) -> None:
        settings = load_config(env={"FLIGHTAGENT__SCORING__DIRECT_BONUS_MODE": "proportional"})
        with pytest.raises(ValueError, match="cheapest_valid_stop_price_eur"):
            score_itinerary(
                _direct_itinerary(),
                scoring_settings=settings.scoring,
                layover_settings=settings.layover,
            )


class TestDirectBonusChangesRankingOutcome:
    """T30's core proof: the bonus is not merely a field with the right
    number in it — it actually changes which itinerary ranks first.

    Constructs a direct itinerary and an otherwise-identical one-stop
    itinerary (same fare, same total duration — a 210min/[180,240)->0
    layover inserted so the one-stop's layover_penalty is also zero, isolating
    the bonus as the only difference). Under the -120.0 fixed default, the
    direct itinerary's adjusted_score must be lower (better) by EXACTLY 120,
    flipping which one would sort first by adjusted_score — proving the bonus
    is load-bearing for ranking, not just numerically present.
    """

    def test_direct_bonus_lowers_adjusted_score_below_equivalent_stop_itinerary(self) -> None:
        fare = Decimal("900.00")

        # Direct: AMS(08:00Z) -> DEL(16:00Z), 8h, stop_count == 0.
        direct_seg = _segment(
            origin="AMS",
            destination="DEL",
            depart_utc=datetime(2027, 7, 17, 8, 0, tzinfo=UTC),
            arrive_utc=datetime(2027, 7, 17, 16, 0, tzinfo=UTC),
            flight_number="872",
        )
        direct_leg = Leg(segments=(direct_seg,), layovers=())
        direct_itinerary = _itinerary(
            legs=(direct_leg,), price_eur=fare, itinerary_id="itin_rank_direct_0001"
        )

        # One-stop, same fare and same 8h total duration: two 4h segments
        # around a 210min (3h30m) layover -- band [180,240) -> 0 penalty,
        # so layover_penalty is 0 on both sides and only direct_bonus differs.
        stop_seg1 = _segment(
            origin="AMS",
            destination="DXB",
            depart_utc=datetime(2027, 7, 17, 8, 0, tzinfo=UTC),
            arrive_utc=datetime(2027, 7, 17, 10, 30, tzinfo=UTC),
            flight_number="431",
        )
        stop_layover = _layover(
            airport="DXB",
            arrive_utc=datetime(2027, 7, 17, 10, 30, tzinfo=UTC),
            depart_utc=datetime(2027, 7, 17, 14, 0, tzinfo=UTC),
        )
        stop_seg2 = _segment(
            origin="DXB",
            destination="DEL",
            depart_utc=datetime(2027, 7, 17, 14, 0, tzinfo=UTC),
            arrive_utc=datetime(2027, 7, 17, 16, 0, tzinfo=UTC),
            flight_number="512",
        )
        stop_leg = Leg(segments=(stop_seg1, stop_seg2), layovers=(stop_layover,))
        stop_itinerary = _itinerary(
            legs=(stop_leg,), price_eur=fare, itinerary_id="itin_rank_stop_0001"
        )
        assert stop_itinerary.total_duration == direct_itinerary.total_duration
        assert stop_itinerary.stop_count == 1

        direct_components = score_itinerary(
            direct_itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        stop_components = score_itinerary(
            stop_itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )

        # Same fare, same duration -> identical layover_penalty (0) and
        # identical elapsed_time_component -> identical pre-bonus `score`.
        assert direct_components.layover_penalty == Decimal("0")
        assert stop_components.layover_penalty == Decimal("0")
        assert direct_components.score == stop_components.score

        # direct_bonus is the ONLY difference, and it is exactly -120.0.
        assert direct_components.direct_bonus == Decimal("-120.0")
        assert stop_components.direct_bonus == Decimal("0")

        # Which flips the ranking: direct's adjusted_score is lower (better)
        # by exactly the bonus amount, so direct now sorts first.
        assert stop_components.adjusted_score - direct_components.adjusted_score == Decimal(
            "120.0"
        )
        assert direct_components.adjusted_score < stop_components.adjusted_score


class TestAllComponentsAreDecimalNeverFloat:
    """Finding 0.3: a type check, not just a value check — float addition
    is not associative, so a component that happens to equal the right
    number as a float would still be the wrong type to build a
    reproducible score from."""

    def test_every_component_and_computed_score_is_decimal(self) -> None:
        components = score_itinerary(
            _worked_example_itinerary(),
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )

        for value in (
            components.fare_eur,
            components.elapsed_time_component,
            components.layover_penalty,
            components.direct_bonus,
            components.score,
            components.adjusted_score,
        ):
            assert isinstance(value, Decimal)
            assert not isinstance(value, float)

    def test_layover_penalty_lookup_returns_decimal(self) -> None:
        penalty = layover_penalty_for_minutes(240, _BANDS)
        assert isinstance(penalty, Decimal)
        assert not isinstance(penalty, float)


class TestLayoverPenaltyForMinutesRejectsOutOfBandInput:
    def test_no_bands_raises(self) -> None:
        with pytest.raises(ValueError, match="no penalty bands configured"):
            layover_penalty_for_minutes(240, [])

    def test_minutes_outside_every_band_raises(self) -> None:
        with pytest.raises(ValueError, match="does not fall within any configured penalty band"):
            layover_penalty_for_minutes(9999, _BANDS)


class TestDurationUsesFractionalHoursNotTruncated:
    """Master plan §10 test-plan item: a fractional-hour duration must
    contribute its true fractional value (13.75 hours * 3.0 EUR/hour =
    41.25), not 39.0 (13 hours truncated * 3.0). Proves
    ``_duration_to_decimal_hours`` carries the fractional remainder into
    the score rather than integer-dividing it away.

    Uses 13h45m rather than the master plan's illustrative "13h50m": 45
    minutes is 45/60 = 0.75 hours, which terminates exactly in base-10
    Decimal, whereas 50/60 = 5/6 is a repeating decimal that even correct
    Decimal division cannot resolve exactly within Python's default 28
    significant-digit context (it lands on
    ``Decimal("41.49999999999999999999999999")``, not literally
    ``Decimal("41.5")``) — a benign, fully deterministic artifact of
    representing a repeating base-10 fraction in fixed precision, not a
    scorer bug, but the wrong choice of minutes for an exact-equality
    assertion. 45 minutes proves the identical point (fractional, not
    truncated) without that footnote."""

    def test_duration_uses_fractional_hours_not_truncated(self) -> None:
        seg = _segment(
            origin="AMS",
            destination="SIN",
            depart_utc=datetime(2027, 7, 17, 8, 0, tzinfo=UTC),
            arrive_utc=datetime(2027, 7, 17, 21, 45, tzinfo=UTC),
            flight_number="633",
        )
        leg = Leg(segments=(seg,), layovers=())
        itinerary = _itinerary(
            legs=(leg,), price_eur=Decimal("500.00"), itinerary_id="itin_frac_hours_0001"
        )
        assert itinerary.total_duration == timedelta(hours=13, minutes=45)

        components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )

        assert components.elapsed_time_component == Decimal("41.25")
        assert components.elapsed_time_component != Decimal("39.0")


class TestZeroPriceItinerary:
    """``fare_eur == 0`` is a legal input (e.g. a fully points-redeemed or
    comped fare) — the scorer must not divide by it, special-case it away,
    or otherwise choke on it. The score must equal exactly the duration and
    layover-penalty components, with nothing contributed by price."""

    def test_zero_price_itinerary_scores_on_duration_alone(self) -> None:
        direct_legs = _direct_itinerary().legs
        itinerary = _itinerary(
            legs=direct_legs, price_eur=Decimal("0"), itinerary_id="itin_zero_price_0001"
        )

        components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )

        assert components.fare_eur == Decimal("0")
        # 8 hours (AMS 08:00Z -> DEL 16:00Z) * 3.0 EUR/hour; zero layovers.
        assert components.elapsed_time_component == Decimal("24.0")
        assert components.layover_penalty == Decimal("0")
        assert components.score == components.elapsed_time_component


class TestScoringReadsTimeValueFromConfigNotHardcoded:
    """The module docstring insists ``time_value_eur_per_hour`` is
    genuinely loaded from config, never a hardcoded ``3.0`` literal — this
    is the test that would catch a silent regression to a hardcoded value.
    Overrides it via ``load_config``'s ``env`` layer (the loader's own
    sanctioned test-substitution mechanism, see ``config.loader``'s
    docstring), never by mutating ``os.environ`` directly, then confirms
    the resulting score actually changes."""

    def test_overriding_time_value_eur_per_hour_changes_the_score(self) -> None:
        overridden_settings = load_config(
            env={"FLIGHTAGENT__SCORING__TIME_VALUE_EUR_PER_HOUR": "7"}
        )
        assert overridden_settings.scoring.time_value_eur_per_hour == Decimal("7")
        assert (
            overridden_settings.scoring.time_value_eur_per_hour
            != _SETTINGS.scoring.time_value_eur_per_hour
        )

        itinerary = _direct_itinerary()  # AMS->DEL, 8h, zero layovers.

        default_components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        overridden_components = score_itinerary(
            itinerary,
            scoring_settings=overridden_settings.scoring,
            layover_settings=overridden_settings.layover,
        )

        # 8h * 3.0 vs 8h * 7 — must differ, and match the new weight
        # exactly, not just differ by some unrelated drift.
        assert default_components.elapsed_time_component == Decimal("24.0")
        assert overridden_components.elapsed_time_component == Decimal("56")
        assert overridden_components.score != default_components.score


class TestScoringReadsPenaltyBandsFromConfigNotHardcoded:
    """Same concern as above, for the D9 penalty band table: constructs a
    ``LayoverSettings`` with a deliberately different band table (a single
    band spanning D8's whole [180,360] window at a nonstandard penalty) and
    confirms ``score_itinerary`` picks up that table rather than any
    packaged ``config/defaults.toml`` band baked in elsewhere."""

    def test_overriding_penalty_bands_changes_the_layover_penalty(self) -> None:
        custom_band = PenaltyBand(min_minutes=180, max_minutes=360, penalty_eur=Decimal("999"))
        custom_layover_settings = LayoverSettings(
            layover_min_minutes=_SETTINGS.layover.layover_min_minutes,
            layover_max_minutes=_SETTINGS.layover.layover_max_minutes,
            penalty_bands=[custom_band],
        )

        itinerary = _worked_example_itinerary()  # one 210min layover -> [180,240)->0 by default.

        default_components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=_SETTINGS.layover,
        )
        overridden_components = score_itinerary(
            itinerary,
            scoring_settings=_SETTINGS.scoring,
            layover_settings=custom_layover_settings,
        )

        assert default_components.layover_penalty == Decimal("0")
        assert overridden_components.layover_penalty == Decimal("999")
        assert overridden_components.layover_penalty != default_components.layover_penalty
